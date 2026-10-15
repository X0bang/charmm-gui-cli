"""Owned local checks that can become builds without restaging chemistry.

Check and modeling-request receipts are separate. Inputs are immutable after
the check; model options are chosen once, before authentication or any POST.
Receipts are never regenerated from changed files. Legacy standalone checks
are intentionally not adopted without an owned task identity and receipt.
"""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import shlex
import uuid

from ..chem.protein_state_proof import canonical
from ..store import tasks
from ..store.auth import ToolError, atomic_json
from ..store.discovery import task_id
from ..store.exclusive_move import move_exclusive
from ..store.json_records import read_object
from ..store.layout import inputs_path, task_path, task_root, log_path, previews_path
from ..store.locking import now, run_lock
from ..store.paths import decode, identity, read_value, _replace, portable
from ..store.workers import claim, metadata, write_lock

REPORT = 'input-check/report.json'
SEAL = 'input-check/seal.json'
REQUEST = 'input-check/model-request.json'


def _command(directory, action='status'):
    name = tasks._job_metadata(Path(directory)).get('name')
    named = isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name)
    if named:
        try:
            named = tasks.resolve_run(name).resolve() == Path(directory).resolve()
        except (OSError, ToolError):
            named = False
    target = name if named else str(task_root(directory))
    return shlex.join(['charmm-gui-cli', 'jobs', action, target])


def build_command(directory):
    target = shlex.split(_command(directory))[-1]
    return shlex.join(['charmm-gui-cli', 'build', '--from-check', target])


def _failure(directory, key='check_task.changed'):
    return ToolError(key=key, category='input_evidence_changed',
                     next_step=shlex.join(['ls', '-l', str(Path(directory) / SEAL)]))


def marked(directory, manifest):
    return ('input_check' in manifest or Path(directory).name == '.charmm-gui'
            and ((Path(directory) / 'input-check').exists() or (Path(directory) / 'input-check').is_symlink()))


def _hash(value, directory):
    return canonical(identity(value, directory))


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _owned(directory, path):
    root = task_root(directory)
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    if ('..' in relative.parts or not path.is_file() or path.is_symlink()
            or any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts)))
            or not path.resolve().is_relative_to(root)):
        raise ValueError
    return path


def _json(directory, path):
    path = _owned(directory, path)
    value, _ = read_object(path, limit=16 * 1024 * 1024)
    return decode(path, value)


def _sources(directory):
    result = {}
    for folder in ('sources', 'component-sources'):
        root = Path(directory) / 'local-check' / folder
        if root.is_symlink():
            raise ValueError
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if path.is_symlink() or len(result) > 10000:
                raise ValueError
            if path.is_file():
                result[str(path.relative_to(directory))] = _digest(_owned(directory, path))
    return result


def _files(directory, prepared, *, passed):
    root = inputs_path(directory)
    files = prepared.get('files', {})
    if not isinstance(files, dict) or root.is_symlink():
        raise ValueError
    for name, value in files.items():
        if (not isinstance(name, str) or Path(name).name != name or '\\' in name
                or _owned(directory, value) != root / name):
            raise ValueError
    if passed:
        if not {'complex.pdb', 'protein.pdb', 'input-manifest.json'} <= files.keys():
            raise ValueError
        actual = _json(directory, files['input-manifest.json'])
        if _hash(actual, directory) != _hash(prepared['manifest'], directory):
            raise ValueError
        from .fragments import record_for
        record_for({'inputs': prepared})
    return {name: _digest(value) for name, value in files.items()}


def _unknown_first(directory):
    from ..store.execution_evidence import submission_evidence
    value = submission_evidence(directory)
    manifest = tasks._read_json(Path(directory) / 'full-run.json')
    if value['unknown'] or value['pending'] or manifest.get('state') == 'submission_unknown':
        from ..remote.api import SubmissionUnknown
        raise SubmissionUnknown(key='build.post_unknown', next_step=_command(directory))


def verify(directory, manifest=None):
    """Read and validate owned check evidence, without repairing or writing it."""
    directory = Path(directory).resolve()
    manifest = manifest if manifest is not None else tasks._read_json(directory / 'full-run.json')
    if not marked(directory, manifest):
        return None
    try:
        if directory.name != '.charmm-gui' or not portable(task_root(directory)):
            raise ValueError
        marker = manifest['input_check']
        if (not isinstance(marker, dict) or type(marker.get('schema_version')) is not int
                or marker['schema_version'] != 1 or marker.get('report') != str(directory / REPORT)
                or marker.get('seal') != str(directory / SEAL)):
            raise ValueError
        report = _json(directory, directory / REPORT)
        seal = _json(directory, directory / SEAL)
        body = {key: value for key, value in seal.items() if key != 'fingerprint'}
        if (set(body) != {'schema_version', 'task_id', 'checked_config', 'inputs_sha256', 'files_sha256', 'report_sha256', 'staged_sources_sha256'}
                or type(body.get('schema_version')) is not int or body['schema_version'] != 1
                or seal.get('fingerprint') != _hash(body, directory) or body['task_id'] != task_id(directory)
                or type(report.get('passed')) is not bool or marker.get('passed') is not report['passed']
                or body['inputs_sha256'] != _hash(manifest['inputs'], directory)
                or body['staged_sources_sha256'] != _sources(directory)
                or body['report_sha256'] != _hash(report, directory)):
            raise ValueError
        actual_files = _files(directory, manifest['inputs'], passed=report['passed'])
        if actual_files != body['files_sha256']:
            changed = sorted(name for name in actual_files if actual_files.get(name) != body['files_sha256'].get(name))
            error = _failure(directory)
            error.details = [{'key': 'check_task.input_changed', 'params': {'path': str(inputs_path(directory) / name)}} for name in changed]
            if changed:
                error.next_step = shlex.join(['ls', '-l', str(inputs_path(directory) / changed[0])])
            raise error
        request = None
        path = directory / REQUEST
        if path.exists() or path.is_symlink():
            request = _json(directory, path)
            request_body = {key: value for key, value in request.items() if key != 'fingerprint'}
            if (set(request_body) != {'schema_version', 'check_fingerprint', 'checked_config_sha256', 'applied_config'}
                    or type(request.get('schema_version')) is not int or request['schema_version'] != 1
                    or request.get('check_fingerprint') != seal['fingerprint']
                    or request['checked_config_sha256'] != _hash(seal['checked_config'], directory)
                    or request.get('fingerprint') != _hash(request_body, directory)):
                raise ValueError
            applied = request['applied_config']
            if (not isinstance(applied, dict)
                    or _hash(_model_config(seal['checked_config'], {key: applied[key] for key in ('membrane', 'ions', 'preparation')}), directory)
                       != _hash(applied, directory)):
                raise ValueError
        if marker.get('model_request_fingerprint') is not None and (request is None or marker['model_request_fingerprint'] != request['fingerprint']):
            raise ValueError
        candidates = [seal['checked_config']] + ([request['applied_config']] if request else [])
        if not any(_hash(manifest['config'], directory) == _hash(value, directory) for value in candidates):
            raise ValueError
        return report, seal, request
    except ToolError as error:
        if error.key and error.key.startswith('check_task.'):
            raise
        raise _failure(directory) from None
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise _failure(directory) from None


def _config(report, prepared, hydrogens):
    from .build_config import validate_model_options
    local = prepared['manifest']
    groups = local.get('chemistry_groups')
    name = groups[0]['resname'] if groups else report['component_resnames'][0] if report['component_resnames'] else 'LIG'
    model = validate_model_options(
        {'upper': 'POPC=1', 'lower': 'POPC=1', 'margin_A': 20, 'water_padding_A': 22.5},
        {'type': 'KCl', 'concentration_M': .15},
        {'orientation': 'ppm', 'n_terminal': 'NTER', 'c_terminal': 'CTER'},
        ligand_resname=name, hydrogens=hydrogens,
        allow_fragments=report.get('input_options', {}).get('split_chain_breaks', False))
    files = prepared['files']
    result = {'version': 2, **model, 'protein': files.get('protein.pdb', ''),
              'ligand': groups[0]['files']['sdf'] if groups else files.get('ligand.sdf', ''),
              'ligand_hydrogens': hydrogens, 'input_options': report.get('input_options', {})}
    if groups:
        result.update(components_mode=True, component_resnames=[group['resname'] for group in groups],
                      components_config=report['input_evidence']['components_config']['path'],
                      component_manifest=files['input-manifest.json'])
    return result


def create(*, name, output=None, preview=True, **options):
    """Run the shared chemical check once; retain one owned task on failure."""
    from ..chem.precheck import check_inputs
    from ..store.result_files import creation_context
    tasks.validate_name(name)
    tasks.check_name_available(name)
    directory = task_path(output) if output is not None else tasks.new_run_path(name)
    root = task_root(directory)
    try:
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        raise ToolError(key='input.check_occupied', params={'path': str(root)},
                        next_step=shlex.join(['ls', '-ld', str(root)])) from None
    directory.mkdir(mode=0o700)
    atomic_json(directory / 'path-mode.json', {'version': 1})
    context = creation_context()
    manifest = {'schema_version': 2, 'created_at': now(), 'state': 'prechecking',
                'delivery_context': context, 'config': {}, 'inputs': {'files': {}, 'manifest': {}},
                'input_check': {'schema_version': 1, 'report': str(directory / REPORT), 'seal': str(directory / SEAL), 'passed': False}}
    tasks._new_json(directory / 'full-run.json', manifest)
    tasks._new_json(directory / 'job.json', {'name': name, 'directory': str(directory), 'task_id': context['task_id'], 'created_at': now()})
    fd = claim(directory)
    initial = {'pid': os.getpid(), 'started_at': now()}
    code = 2
    log = reporter = None
    try:
        write_lock(fd, initial)
        log = log_path(directory).open('x', encoding='utf-8')
        os.chmod(log_path(directory), 0o600)
        from ..presentation.progress import ProgressReporter
        reporter = ProgressReporter(stream=log)
        reporter({'state': 'preparing_inputs (local)'})
        report = check_inputs(out=directory / 'local-check', **options)
        if report['passed'] and any(_digest(item['path']) != item['sha256'] for item in report['input_evidence'].values()):
            raise _failure(directory)
        old = directory / 'local-check/prepared'
        target = inputs_path(directory)
        if old.is_dir() and not old.is_symlink():
            move_exclusive(old, target)
            report = _replace(report, old, target)
        component_source = directory / 'local-check/component-sources'
        if (target / 'sources').exists():
            move_exclusive(target / 'sources', component_source)
            report = _replace(report, target / 'sources', component_source)
        for parent in (target, component_source):
            for path in parent.rglob('*.json'):
                value = _replace(read_value(path), old, target)
                value = _replace(value, target / 'sources', component_source)
                atomic_json(path, value)
        files = report.get('prepared_files', {})
        local = {}
        if files:
            path = Path(files['input-manifest.json'])
            local = read_value(path)
            if 'component_preparation' in report:
                report['component_preparation'] = local
        prepared = {'files': files, 'manifest': local}
        report.update(name=name, directory=str(root), check_file=str(root / 'check.json'), task_registered=True,
                      input_check_file=str(directory / REPORT))
        report.pop('preview', None)
        manifest.update(state='inputs_ready' if report['passed'] else 'local_failed', inputs=prepared,
                        config=_config(report, prepared, options.get('hydrogens', 'add_missing')),
                        input_check={**manifest['input_check'], 'passed': report['passed']})
        if not report['passed']:
            manifest.update(execution_state='failed_local', last_error=report.get('error', {}))
        atomic_json(directory / 'full-run.json', manifest)
        tasks._new_json(directory / REPORT, report)
        body = {'schema_version': 1, 'task_id': context['task_id'], 'checked_config': manifest['config'],
                'inputs_sha256': _hash(prepared, directory), 'files_sha256': _files(directory, prepared, passed=report['passed']),
                'report_sha256': _hash(report, directory), 'staged_sources_sha256': _sources(directory)}
        tasks._new_json(directory / SEAL, {**body, 'fingerprint': _hash(body, directory)})
        tasks.assign_name(directory, name)
        report['next_step'] = build_command(directory) if report['passed'] else report.get('error', {}).get('next_step', _command(directory))
        if preview and report['passed']:
            try:
                report['preview'] = preview_checked(directory)
            except (ToolError, OSError) as error:
                report['preview'] = {'status': 'failed', 'message': str(error)}
        atomic_json(root / 'check.json', report)
        code = 0 if report['passed'] else 2
        from datetime import datetime
        from ..locales.i18n import t
        print('[' + datetime.now().strftime('%H:%M:%S') + '] ' + t('check_task.log_result', state=t('output.passed' if report['passed'] else 'output.failed')), file=log, flush=True)
        return report
    except KeyboardInterrupt as interrupted:
        code = 130
        interrupted.check_directory = str(root)
        raise
    finally:
        try:
            if reporter is not None:
                reporter.close()
            if log is not None:
                log.close()
            write_lock(fd, {**initial, 'finished_at': now(), 'exit_code': code})
        finally:
            os.close(fd)


def _model_config(config, model):
    from .build_config import validate_model_options
    result = deepcopy(config)
    selected = validate_model_options(model['membrane'], model['ions'], model['preparation'],
        ligand_resname=config['ligand_resname'], hydrogens=config['ligand_hydrogens'],
        allow_fragments=config.get('input_options', {}).get('split_chain_breaks', False))
    result.update(selected)
    return result


def choose_model(directory, *, model=None):
    """Choose one request locally; resume a recorded local commit, never reseal."""
    directory = Path(directory).resolve()
    if not (directory / 'full-run.json').exists() and not (directory / 'input-check').exists() and model is None:
        return None  # Preserve legacy callers which resolve/read their run later.
    manifest = tasks._read_json(directory / 'full-run.json')
    if not marked(directory, manifest):
        if model is not None:
            raise _failure(directory, 'check_task.not_owned')
        return manifest
    _unknown_first(directory)
    report, seal, request = verify(directory, manifest)
    if not report['passed']:
        raise ToolError(key='check_task.failed', next_step=report.get('error', {}).get('next_step') or _command(directory))
    proposed = _model_config(seal['checked_config'], model) if model is not None else request['applied_config'] if request else seal['checked_config']
    if request and _hash(proposed, directory) != _hash(request['applied_config'], directory):
        raise ToolError(key='check_task.request_fixed', next_step=_command(directory, 'resume'))
    if (request and manifest['input_check'].get('model_request_fingerprint') == request['fingerprint']
            and _hash(manifest['config'], directory) == _hash(request['applied_config'], directory)):
        require_requested(directory, manifest)
        return manifest
    if metadata(directory)['running']:
        raise ToolError(key='background.running', next_step=_command(directory, 'watch'))
    fd = claim(directory)
    try:
        with run_lock(directory):
            manifest = tasks._read_json(directory / 'full-run.json')
            report, seal, request = verify(directory, manifest)
            from ..store.local_build import _remote_evidence
            if _remote_evidence(directory):
                raise _failure(directory)
            from .build_config import validate_terminal_residues
            from ..chem.fragment_inputs import validate_prepared_terminals
            if not validate_prepared_terminals(manifest['inputs'], proposed['preparation']):
                validate_terminal_residues(proposed['preparation'], report.get('terminal_predictions', []))
            from .full_build import _remote_groups
            _remote_groups({**manifest, 'config': proposed})
            if request is None:
                body = {'schema_version': 1, 'check_fingerprint': seal['fingerprint'],
                        'checked_config_sha256': _hash(seal['checked_config'], directory), 'applied_config': proposed}
                request = {**body, 'fingerprint': _hash(body, directory)}
                tasks._new_json(directory / REQUEST, request)
            elif _hash(proposed, directory) != _hash(request['applied_config'], directory):
                raise _failure(directory)
            manifest['config'] = request['applied_config']
            from .fragments import seal_local_inputs
            seal_local_inputs(directory, manifest, create=True)
            manifest['input_check']['model_request_fingerprint'] = request['fingerprint']
            atomic_json(directory / 'full-run.json', manifest)
            return manifest
    finally:
        os.close(fd)


def require_requested(directory, manifest):
    if not marked(directory, manifest):
        return
    _unknown_first(directory)
    report, _, request = verify(directory, manifest)
    if (not report['passed'] or request is None or manifest['input_check'].get('model_request_fingerprint') != request['fingerprint']
            or _hash(manifest['config'], directory) != _hash(request['applied_config'], directory)):
        raise ToolError(key='check_task.request_needed', next_step=build_command(directory))
    from .fragments import seal_local_inputs
    seal_local_inputs(directory, manifest)


def from_args(args):
    """Resolve the explicit --from-check route without reading original files."""
    from ..chem.residues import INPUT_FIELDS
    reference = getattr(args, 'from_check', None)
    if not reference:
        return None
    sources = ('protein', 'ligand', 'complex_pdb', 'components', 'ligand_resname', 'ligand_smiles', 'hydrogens', *INPUT_FIELDS)
    if (getattr(args, 'config', None) or getattr(args, 'accept_conect_bond_orders', False)
            or any(getattr(args, key, None) is not None for key in sources)):
        raise ToolError(key='check_task.mixed_sources', next_step='charmm-gui-cli build --help')
    directory = tasks.resolve_run(reference)
    manifest = tasks._read_json(directory / 'full-run.json')
    if not marked(directory, manifest):
        raise _failure(directory, 'check_task.not_owned')
    if metadata(directory)['running']:
        raise ToolError(key='background.running', next_step=_command(directory, 'watch'))
    if (getattr(args, 'out', None) is not None and task_path(args.out) != directory
            or getattr(args, 'name', None) not in (None, tasks._job_metadata(directory).get('name'))):
        raise ToolError(key='check_task.location_fixed', next_step=build_command(directory))
    _unknown_first(directory)
    checked = verify(directory, manifest)
    base = checked[2]['applied_config'] if checked[2] else checked[1]['checked_config']
    selected = {key: deepcopy(base[key]) for key in ('membrane', 'ions', 'preparation')}
    for group, names in (
        ('membrane', {'upper': 'upper', 'lower': 'lower', 'margin': 'margin_A', 'water_padding': 'water_padding_A', 'allow_unknown_lipids': 'allow_unknown_lipids'}),
        ('ions', {'salt': 'type', 'salt_concentration': 'concentration_M'}),
        ('preparation', {'orientation': 'orientation', 'n_terminal': 'n_terminal', 'c_terminal': 'c_terminal', 'terminals': 'terminals'})):
        for key, target in names.items():
            value = getattr(args, key, None)
            if value is not None:
                selected[group][target] = value
    choose_model(directory, model=selected)
    return directory


def inspect_checked(directory, manifest=None):
    """Return a scoped check summary, never a final model-acceptance result."""
    checked = verify(directory, manifest)
    if checked is None:
        return None
    report, _, request = checked
    result = dict(report)
    result['next_step'] = _command(directory, 'resume') if request else build_command(directory)
    cache = Path(directory) / 'input-check/preview.json'
    if cache.is_file():
        try:
            preview = _json(directory, cache)
            if _valid_preview(directory, preview, report['prepared_complex']):
                result['preview'] = preview
        except (OSError, ValueError, TypeError, KeyError, ToolError):
            pass
    return result


def status_fields(directory, manifest=None):
    from ..presentation.summaries import precheck_summary
    from ..store.local_build import _remote_evidence
    directory = Path(directory)
    manifest = manifest if manifest is not None else tasks._read_json(directory / 'full-run.json')
    if not marked(directory, manifest):
        return {}
    local_only = not _remote_evidence(directory) and not (directory / 'results/validation.json').exists()
    if manifest.get('state') == 'prechecking' and metadata(directory)['running']:
        command = shlex.join(['charmm-gui-cli', 'jobs', 'watch', str(task_root(directory))])
        return {'precheck_only': True, 'input_check_status': 'checking', 'model_requested': False,
                'input_check': {'status': 'checking', 'passed': None, 'next_step': command}, 'next_step': command}
    try:
        report = inspect_checked(directory, manifest)
        state = 'passed' if report['passed'] else 'failed'
    except ToolError as error:
        state = 'stale'
        report = {'passed': False, 'status': 'stale', 'directory': str(task_root(directory)),
                  'check_file': str(task_root(directory) / 'check.json'), 'error': error.as_dict(),
                  'scientific_correctness_verified': False, 'next_step': error.next_step}
    result = {'precheck_only': local_only, 'input_check_status': state,
              'input_check': precheck_summary(report), 'model_requested': (directory / REQUEST).is_file()}
    if local_only and (state != 'passed' or not manifest.get('failure') and not manifest.get('last_error')):
        result['next_step'] = result['input_check']['next_step']
    return result


def write_report(directory, *, format, full=False):
    """Return a local report only while this task has no remote model evidence."""
    # Named checks require the modern owned-task layout. Do not consume an
    # extra manifest read (or change reporting behavior) for legacy tasks.
    if Path(directory).name != '.charmm-gui':
        return None
    value = status_fields(directory)
    if not value.get('precheck_only'):
        return None
    report = inspect_checked(directory)
    from ..presentation.summaries import precheck_summary
    if format == 'json':
        return report if full else precheck_summary(report)
    from ..locales.i18n import t
    from ..presentation.terminal_output import format_check
    text = format_check(precheck_summary(report)) + '\n\n' + t('check_task.report_scope') + '\n'
    if format == 'text':
        return text
    from html import escape
    title = escape(t('check_task.report_title'))
    return ('<!doctype html><html><head><meta charset="utf-8"><title>' + title
            + '</title></head><body><h1>' + title + '</h1><pre>' + escape(text) + '</pre></body></html>')


def _valid_preview(directory, preview, source):
    return (preview.get('scope') == 'prepared_input' and preview.get('model_copied') is False
            and preview.get('source_sha256') == _digest(_owned(directory, source))
            and _owned(directory, preview['model']) == _owned(directory, source)
            and 1 <= len(preview.get('images', [])) <= 3
            and all(_owned(directory, item['file']).is_relative_to(previews_path(directory))
                    and Path(item['file']).suffix == '.png' and item.get('view') in ('side', 'top', 'ligand')
                    and _digest(_owned(directory, item['file'])) == item['sha256'] for item in preview['images']))


def preview_checked(directory, *, out=None, width=1400, views=('side', 'top', 'ligand')):
    from ..chem.preview import render_model
    directory = Path(directory).resolve()
    report = verify(directory)[0]
    if not report['passed']:
        raise ToolError(key='check_task.failed', next_step=report.get('error', {}).get('next_step') or _command(directory))
    cache = directory / 'input-check/preview.json'
    if out is None and cache.is_file():
        try:
            previous = _json(directory, cache)
            if previous.get('requested_width') == width and previous.get('requested_views') == list(views) and _valid_preview(directory, previous, report['prepared_complex']):
                return previous
        except (OSError, ValueError, TypeError, KeyError, ToolError):
            pass
    destination = Path(out) if out is not None else previews_path(directory) / ('input-' + uuid.uuid4().hex[:12])
    rendered = render_model(report['prepared_complex'], destination, ligand_resnames=report['component_resnames'],
                            width=width, views=views, scope='prepared_input', copy_model=False)
    rendered.update(requested_width=width, requested_views=list(views))
    if out is None:
        atomic_json(cache, rendered)
    return rendered

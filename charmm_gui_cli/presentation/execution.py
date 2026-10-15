"""Read-only execution semantics, independent of historical pipeline stages."""

from datetime import datetime
from pathlib import Path
import re
import shlex

from ..store.layout import task_root


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def describe_execution(value, *, target=None, legacy_error=None):
    """Add diagnostics; do not change a saved state, category, intent or verdict."""
    state = value.get('state')
    background = _dict(value.get('background'))
    running = background.get('running') is True
    recovery = _dict(value.get('recovery'))
    error = _dict(recovery.get('error')) or _dict(value.get('last_error')) or _dict(legacy_error)
    error_origin = 'recovery' if _dict(recovery.get('error')) else 'summary' if _dict(value.get('last_error')) else 'legacy_manifest' if _dict(legacy_error) else None
    category = error.get('category')
    native = value.get('execution_state')
    evidence = _dict(value.get('submission_evidence'))
    unknown = state == 'submission_unknown' or native == 'submission_unknown' or category == 'submission_unknown' or evidence.get('unknown') is True
    pending = evidence.get('pending') is True or state in ('submitting', 'upload_submitting')
    unknown = unknown or pending and not running
    start = _time(background.get('started_at'))
    failed = _time(recovery.get('updated_at') or error.get('at'))
    prior_error = running and not unknown and (
        start is not None and failed is not None and failed < start
        or failed is None and native == 'interrupted' and category == 'interrupted' and state != 'interrupted')
    current_error = bool(error) and not prior_error
    if prior_error:
        error, category, native = {}, None, None
    if target is None:
        name = value.get('name')
        target = name if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name) else None
        if target is None and isinstance(value.get('directory'), str):
            target = str(task_root(Path(value['directory'])))
    elif '/' in str(target) or str(target).startswith('~'):
        target = str(task_root(Path(target).expanduser()))
    base = ['charmm-gui-cli', 'jobs']
    resume = shlex.join([*base, 'resume', str(target)]) if target is not None else 'charmm-gui-cli jobs list'
    local_status = shlex.join([*base, 'status', str(target)]) if target is not None else 'charmm-gui-cli jobs list'
    next_step = resume
    if unknown:
        outcome, reason = 'submission_unknown', 'unknown_submission'
        next_step = shlex.join([*base, 'reconcile', str(target)]) if target is not None else local_status
        if evidence.get('unreadable') and evidence.get('records'):
            reason = 'unreadable_submission'
            next_step = shlex.join(['ls', '-ld', evidence['records'][0]['path']])
        if error.get('cause_category') in ('authentication', 'auth_required', 'access_denied'):
            next_step = 'charmm-gui-cli login; ' + next_step
    elif value.get('state') == 'validated' and value.get('passed') is True and not value.get('validation_status'):
        outcome, reason = 'passed', 'verified_report'
        next_step = shlex.join([*base, 'report', str(target)]) if target is not None else local_status
    elif value.get('validation_status') == 'stale_or_unreadable':
        outcome, reason = 'failed_local', 'stale_validation'
    elif state in ('remote_error', 'preparation_failed') or category in ('remote_failure', 'remote_error', 'preparation_failed', 'api_error') or value.get('server_checked') is True and value.get('remote_status') == 'error':
        outcome, reason = 'failed_remote', 'recorded_remote_failure'
        next_step = local_status
    elif current_error and category in ('authentication', 'auth_required', 'access_denied'):
        outcome, reason = 'paused', 'authentication'
        next_step = 'charmm-gui-cli login; ' + resume
    elif current_error and category in ('transient_network', 'transient_http', 'transport_error', 'rate_limited', 'http_error', 'unexpected_response', 'wait_timeout', 'archive_preparing'):
        outcome, reason = 'paused', 'request_failed'
    elif native == 'failed_remote':
        outcome, reason = 'failed_remote', 'recorded_remote_failure'
        next_step = local_status
    elif state in ('validation_failed', 'local_failed') or native == 'failed_local' or current_error and error.get('retryable') is False:
        outcome, reason = 'failed_local', 'recorded_local_failure'
    elif running:
        outcome, reason = 'running', 'active_lease'
        next_step = shlex.join([*base, 'watch', str(target)]) if target is not None else local_status
    elif native == 'interrupted' or state == 'interrupted' or category == 'interrupted':
        outcome, reason = 'interrupted', 'recorded_interruption'
    elif current_error:
        outcome, reason = 'paused', 'recorded_error'
    elif background.get('pid') and not background.get('finished_at'):
        outcome, reason = 'interrupted', 'worker_missing'
    elif type(background.get('exit_code')) is int and background['exit_code'] != 0:
        outcome, reason = ('interrupted', 'recorded_interruption') if background['exit_code'] < 0 or background['exit_code'] in (130, 137, 143) else ('failed_local', 'worker_exit')
    elif state == 'validated' or state in ('done', 'downloaded_unverified', 'ligands_present') or value.get('server_checked') is True and value.get('remote_status') == 'done':
        outcome, reason = 'needs_validation', 'validation_needed'
    elif state in ('inputs_ready', 'prepared_reused', 'pending') and not value.get('build_job_id'):
        outcome, reason = 'ready', 'local_inputs'
    else:
        outcome, reason = 'stopped', 'no_active_worker'
    unbounded = bool(background.get('started_at') and not background.get('finished_at') and not running)
    return {'outcome': outcome, 'reason_key': 'execution.reason.' + reason,
            'local_running': running, 'remote_checked': value.get('server_checked') is True,
            'error_current': current_error, 'prior_error': prior_error, 'error_origin': error_origin,
            'elapsed_is_duration': not unbounded, 'next_step': next_step}

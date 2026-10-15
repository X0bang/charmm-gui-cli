"""Bilingual requested/submitted parameter views, never raw website controls."""

from ..locales.i18n import t


def _number(value):
    return f'{value:.4g}' if isinstance(value, (int, float)) and not isinstance(value, bool) else t('settings.not_recorded')


def _mixture(rows):
    if not rows:
        return t('settings.not_recorded')
    return t('settings.mixture', names=':'.join(row['lipid'] for row in rows),
             parts=':'.join(_number(row['parts']) for row in rows),
             percentages=':'.join(_number(100 * row['fraction']) + '%' for row in rows))


def _model(settings):
    membrane, ions = settings.get('membrane', {}), settings.get('ions', {})
    orientation = settings.get('orientation')
    return [t('report.leaflets', upper=_mixture(membrane.get('upper')), lower=_mixture(membrane.get('lower'))),
            t('report.salt', salt=ions.get('type') or t('settings.not_recorded'), concentration=_number(ions.get('concentration_M')),
              orientation=t('settings.orientation.' + orientation) if orientation in ('ppm', 'prepared') else t('settings.not_recorded')),
            t('report.padding', margin=_number(membrane.get('margin_A')), water=_number(membrane.get('water_padding_A')))]


def _terminal(value):
    return value or t('settings.not_recorded')


def _change(requested, submitted):
    if requested and submitted and requested != submitted:
        return t('settings.change', requested=requested, submitted=submitted)
    return _terminal(submitted)


def parameter_lines(summary):
    if not isinstance(summary, dict) or summary.get('schema_version') != 1:
        return []
    requested = summary['requested']
    lines = [t('settings.requested'), *_model(requested)]
    defaults = requested['terminals']['defaults']
    if any(defaults.values()):
        lines.append(t('settings.defaults', n=_terminal(defaults.get('n_terminal')), c=_terminal(defaults.get('c_terminal'))))
    overrides = requested['terminals']['chains']
    for row in overrides[:20]:
        lines.append(t('settings.request_chain', chain=row['chain'], n=_terminal(row.get('n_terminal')),
                       c=_terminal(row.get('c_terminal'))))
    if len(overrides) > 20:
        lines.append(t('settings.more_chains', count=len(overrides) - 20))
    preparation = summary['submission']['preparation']
    status, rows = preparation['status'], preparation['chains']
    label = {'response_received': 'submitted', 'recorded': 'recorded', 'submission_unknown': 'unknown',
             'adjustment_record': 'adjustments', 'missing': 'missing', 'unavailable': 'unavailable',
             'invalid_record': 'invalid', 'conflicting_record': 'invalid'}.get(status, 'invalid')
    lines.append(t('settings.terminals.' + label))
    for row in rows[:20]:
        chain = row['chain']
        if 'prepared_chain' in row:
            source = row['source_chain']
            chain = t('fragment.chain_label', chain=chain, fragment=row['prepared_chain'],
                      source=source.get('segment') or source.get('chain') or str(source['polymer_block']))
        lines.append(t('settings.submitted_chain', chain=chain,
            n=_change(row.get('requested_n_terminal'), row.get('n_terminal')),
            c=_change(row.get('requested_c_terminal'), row.get('c_terminal'))))
    if len(rows) > 20:
        lines.append(t('settings.more_chains', count=len(rows) - 20))
    membrane = summary['submission']['membrane']
    settings = membrane.get('settings')
    if settings:
        # Requested terminal overrides are a separate section, not API lipids.
        same = settings == {key: value for key, value in requested.items() if key != 'terminals'}
        if membrane['status'] == 'response_received':
            lines.append(t('settings.membrane_same' if same else 'settings.membrane_submitted'))
            if not same:
                lines.extend(_model(settings))
        else:
            key = ('settings.membrane_unknown' if membrane['status'] == 'submission_unknown' else
                   'settings.membrane_invalid' if membrane['status'] in ('invalid_record', 'conflicting_record') else 'settings.membrane_recorded')
            lines.append(t(key))
            lines.extend(_model(settings))
    return lines

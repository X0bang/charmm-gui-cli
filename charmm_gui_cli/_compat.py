"""Explicit legacy Python imports; internal code must use canonical modules.

The compatibility registry is not an import hook. It is consulted only when
an external caller explicitly imports one of the small legacy shim files.
"""

from importlib import import_module
import sys

MODULE_ALIASES = {
    'charmm_gui_cli.acceptance': 'charmm_gui_cli.pipeline.acceptance',
    'charmm_gui_cli.api': 'charmm_gui_cli.remote.api',
    'charmm_gui_cli.archive_transport': 'charmm_gui_cli.remote.archive_transport',
    'charmm_gui_cli.artifacts': 'charmm_gui_cli.pipeline.artifacts',
    'charmm_gui_cli.auth': 'charmm_gui_cli.store.auth',
    'charmm_gui_cli.background': 'charmm_gui_cli.store.background',
    'charmm_gui_cli.batch': 'charmm_gui_cli.pipeline.batch',
    'charmm_gui_cli.build_command': 'charmm_gui_cli.pipeline.build_command',
    'charmm_gui_cli.build_config': 'charmm_gui_cli.pipeline.build_config',
    'charmm_gui_cli.config': 'charmm_gui_cli.pipeline.config',
    'charmm_gui_cli.delivery': 'charmm_gui_cli.store.delivery',
    'charmm_gui_cli.doctor': 'charmm_gui_cli.store.doctor',
    'charmm_gui_cli.full_build': 'charmm_gui_cli.pipeline.full_build',
    'charmm_gui_cli.helptext': 'charmm_gui_cli.locales.helptext',
    'charmm_gui_cli.http_retry': 'charmm_gui_cli.remote.http_retry',
    'charmm_gui_cli.i18n': 'charmm_gui_cli.locales.i18n',
    'charmm_gui_cli.input_sources': 'charmm_gui_cli.chem.input_sources',
    'charmm_gui_cli.inputs': 'charmm_gui_cli.chem.inputs',
    'charmm_gui_cli.layout': 'charmm_gui_cli.store.layout',
    'charmm_gui_cli.managed': 'charmm_gui_cli.store.managed',
    'charmm_gui_cli.multicomponent': 'charmm_gui_cli.chem.multicomponent',
    'charmm_gui_cli.pdb_normalization': 'charmm_gui_cli.chem.pdb_normalization',
    'charmm_gui_cli.pipeline.archive_transport': 'charmm_gui_cli.remote.archive_transport',
    'charmm_gui_cli.pipeline.progress': 'charmm_gui_cli.presentation.progress',
    'charmm_gui_cli.pose_validation': 'charmm_gui_cli.chem.pose_validation',
    'charmm_gui_cli.precheck': 'charmm_gui_cli.chem.precheck',
    'charmm_gui_cli.preview': 'charmm_gui_cli.chem.preview',
    'charmm_gui_cli.progress': 'charmm_gui_cli.presentation.progress',
    'charmm_gui_cli.reconcile': 'charmm_gui_cli.remote.reconcile',
    'charmm_gui_cli.reporting': 'charmm_gui_cli.presentation.reporting',
    'charmm_gui_cli.run_logging': 'charmm_gui_cli.store.run_logging',
    'charmm_gui_cli.structure': 'charmm_gui_cli.chem.structure',
    'charmm_gui_cli.sweep': 'charmm_gui_cli.pipeline.sweep',
    'charmm_gui_cli.terminal_output': 'charmm_gui_cli.presentation.terminal_output',
    'charmm_gui_cli.updater': 'charmm_gui_cli.pipeline.updater',
    'charmm_gui_cli.validation': 'charmm_gui_cli.chem.validation',
    'charmm_gui_cli.web': 'charmm_gui_cli.remote.web',
    'charmm_gui_cli.web_state': 'charmm_gui_cli.remote.web_state',
    'charmm_gui_cli.workflow': 'charmm_gui_cli.pipeline.workflow',
}


def install_alias(name):
    module = import_module(MODULE_ALIASES[name])
    sys.modules[name] = module
    return module

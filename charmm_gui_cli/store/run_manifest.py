"""Strict historical API-run records, independent of orchestration."""

from pathlib import Path
from .auth import ToolError
from .paths import read_value
from .configuration import validate_config
from .identifiers import job_id

def read(directory):
    try:
        manifest = read_value(Path(directory) / "run.json")
    except (OSError, ValueError):
        raise ToolError("Cannot read run.json in the run directory.") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ToolError("Unsupported run manifest.")
    # New catalog coverage must not invalidate an existing recorded task.
    validate_config(manifest.get("config"), existing=True)
    if manifest.get("job_id") is not None:
        job_id(manifest["job_id"])
    return manifest

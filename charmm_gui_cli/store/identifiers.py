"""Validated CHARMM-GUI job identifiers without HTTP dependencies."""

import re
from .auth import ToolError

def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
        raise ToolError(key="api.invalid_jobid")
    return value

"""Read optional viewer metadata without trusting arbitrary saved fields."""

import json
from pathlib import Path
import re


def saved_transfer(directory):
    for path in (Path(directory) / "download-progress.json", Path(directory) / "bilayer/download-progress.json"):
        try:
            if path.is_symlink():
                continue
            value = json.loads(path.read_text())
            received = value.get("downloaded_bytes") if isinstance(value, dict) else None
            if (isinstance(value, dict) and value.get("active") is True and type(received) is int and 0 <= received < 2**63
                    and isinstance(value.get("job_id"), str) and re.fullmatch(r"[0-9]{1,32}", value["job_id"])):
                result = {"active": True, "downloaded_bytes": received, "job_id": value["job_id"]}
                total = value.get("total_bytes")
                result["total_bytes"] = total if type(total) is int and 0 < total < 2**63 else None
                if type(value.get("transfer_complete")) is bool:
                    result["transfer_complete"] = value["transfer_complete"]
                stamp = value.get("updated_at")
                if isinstance(stamp, str) and re.fullmatch(r"[0-9T:.+Z-]{1,40}", stamp):
                    result["updated_at"] = stamp
                return result
        except (OSError, ValueError):
            pass
    return None

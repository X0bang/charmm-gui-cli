"""Optional local desktop notifications; no network, shell or task mutation."""

import shutil
import subprocess
import sys

from ..locales.i18n import t


def notify_completion(code):
    """Best effort only; missing desktop services must not change the result."""
    message = t("background.notification_ok" if code == 0 else "background.notification_failed")
    if sys.platform == "darwin":
        command = ["osascript", "-e", 'on run argv\ndisplay notification (item 1 of argv) with title "charmm-gui-cli"\nend run', message]
    elif sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        if not executable:
            return False
        command = [executable, "--app-name=charmm-gui-cli", "--expire-time=10000", "--", "charmm-gui-cli", message]
    else:
        return False
    try:
        result = subprocess.run(command, timeout=5, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

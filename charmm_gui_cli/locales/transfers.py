"""Localized transfer units shared by terminal renderers."""

from .i18n import t


def transfer_text(value):
    received, total = value.get("downloaded_bytes"), value.get("total_bytes")
    if type(received) is not int or not 0 <= received < 2**63:
        return None
    def size(number):
        return f"{number} B" if number < 1024 else f"{number / 1024:.1f} KiB" if number < 1048576 else f"{number / 1048576:.1f} MiB"
    actual = size(received)
    return t("download.bytes", received=actual, total=size(total)) if type(total) is int and 0 < total < 2**63 else t("download.bytes_unknown", received=actual)

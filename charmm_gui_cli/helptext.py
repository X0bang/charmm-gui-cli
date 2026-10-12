"""Dynamic localized workflow help; importing it never accesses the network."""

from .i18n import t


def __getattr__(name):
    if name in ("OVERVIEW", "BUILD_GUIDE"):
        return t("help." + name.lower())
    raise AttributeError(name)

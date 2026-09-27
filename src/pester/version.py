"""The running version, from the installed package (the image's tag, for released images)."""

from importlib.metadata import PackageNotFoundError, version


def _installed_version() -> str:
    try:
        return version("pester")
    except PackageNotFoundError:  # running from a source tree without installing
        return "0+unknown"


VERSION = _installed_version()

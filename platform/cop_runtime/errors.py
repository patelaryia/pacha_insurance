"""Public fail-closed COP runtime errors."""


class PackLoadError(RuntimeError):
    """A pack failed validation and was not registered."""


__all__ = ["PackLoadError"]

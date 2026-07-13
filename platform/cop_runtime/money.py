"""Money primitives shared by the COP runtime and pack calculations."""

from typing import NewType

Money = NewType("Money", int)

__all__ = ["Money"]

"""Integration suites.

This directory is a package purely to give its modules distinct import names.
The Temporal master plan names both `tests/unit/test_temporal_orchestration.py`
and `tests/integration/test_temporal_orchestration.py`, and pytest's default
import mode cannot hold two same-named modules from two non-package directories.
"""

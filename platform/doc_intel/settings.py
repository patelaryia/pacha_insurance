"""Strict loader for Packet-04 defaults that must remain configuration data."""

from pathlib import Path
from typing import Any

import yaml


def load_defaults() -> dict[str, Any]:
    payload = yaml.safe_load(Path(__file__).with_name("defaults.yaml").read_text())
    if not isinstance(payload, dict):
        raise RuntimeError("doc-intel defaults must be a mapping")
    return payload


DEFAULTS = load_defaults()

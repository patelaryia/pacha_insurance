"""Meta-tests: the Packet-0 guards must catch the invariants they enforce.

These prove the ED-8 money-float lint and the AR-2 banned-call check fail on a
violation and pass on clean input, so a green CI run is a real signal.
"""
from __future__ import annotations

import importlib.util
import pathlib

_CI = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ci"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _CI / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


money = _load("money_float_lint")
banned = _load("banned_calls")


# --- ED-8 money-float ---------------------------------------------------------

def test_money_float_flags_float_in_money_signature():
    src = "def payable(reserve: Money, ratio: float) -> Money: ...\n"
    assert money.check_source(src, "x.py")


def test_money_float_flags_money_aliased_to_float():
    assert money.check_source("Money = float\n", "x.py")


def test_money_float_passes_clean_signature():
    src = "def payable(reserve: Money, ratio: int) -> Money: ...\n"
    assert money.check_source(src, "x.py") == []


# --- AR-2 banned-calls --------------------------------------------------------

def test_banned_flags_graph_send():
    assert banned.scan_text("graph_client.send(msg)\n")


def test_banned_flags_adapter_execute():
    assert banned.scan_text("adapter.execute(op)\n")


def test_banned_ignores_comment():
    assert banned.scan_text("# graph_client.send is banned here\n") == []


def test_banned_gate_module_is_exempt():
    text = "def execute_or_stage(op):\n    return adapter.execute(op)\n"
    assert banned.is_exempt(pathlib.Path("platform/gate/gate.py"), text) is True


def test_banned_notify_dir_is_exempt():
    assert banned.is_exempt(pathlib.Path("platform/notify/send.py"), "graph_client.send(x)") is True

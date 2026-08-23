"""Guard: backend modules must never shadow Python standard-library names.

Regression history: a file literally named `secrets.py` broke TOTP generation
(`secrets.token_bytes` vanished). This canary fails fast on any repeat.
"""
import importlib
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

STDLIB_CANARIES = ["secrets", "statistics", "json", "types", "logging", "random"]


def test_canary_modules_still_stdlib():
    for name in STDLIB_CANARIES:
        mod = importlib.import_module(name)
        origin = getattr(mod, "__file__", "") or ""
        assert "site-packages" in origin or "lib/python" in origin, \
            f"{name!r} resolved outside the standard library: {origin}"


def test_backend_file_names_do_not_collide_with_stdlib():
    import sysconfig
    std_dir = Path(sysconfig.get_paths()["stdlib"])
    std_names = {p.stem.lower() for p in std_dir.glob("*.py")}
    local = [f.name for f in BACKEND.glob("*.py")] + \
        [f.stem for f in (BACKEND / "agents").glob("*.py") if f.stem != "__init__"]
    bad = sorted({n for n in local if n.lower() in std_names})
    assert not bad, f"backend modules shadow stdlib: {bad}"

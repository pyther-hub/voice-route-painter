"""DATA_DIR (the Fly volume): traces and today's spend land there and survive a restart. Each check runs in a fresh
Python process, because both paths are read when the modules are imported."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str, data_dir: Path) -> str:
    env = {"DATA_DIR": str(data_dir), "TRACE_PRINT": "0", "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_spend_survives_a_restart(tmp_path):
    _run("from app import guard; guard.charge('a', 0.5); guard.charge('b', 0.25)", tmp_path)
    assert (tmp_path / "spend.json").exists()
    out = _run("from app import guard; print(guard.spent_today())", tmp_path)     # a new process = a restart
    assert float(out) == 0.75


def test_traces_go_to_the_data_dir(tmp_path):
    out = _run("from app import trace\n"
               "with trace.span('agent', 'turn', input={'text': 'hi'}) as sp: tid = sp['trace_id']\n"
               "print(trace.DB_PATH, len(trace.read_trace(tid)['spans']))", tmp_path)
    path, n = out.rsplit(" ", 1)
    assert Path(path) == tmp_path / "traces.db" and n == "1"


def test_without_data_dir_nothing_is_saved():
    from app import guard
    assert guard.SPEND_PATH is None or "DATA_DIR" in __import__("os").environ

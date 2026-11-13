"""文字列表の実行境界を、実判定と実プロセスで確かめる。"""

import importlib.util
import json
import runpy
from pathlib import Path
import os
import subprocess
import sys

import pytest

from test_review_3c import GREEN, RED, matrix_message, message, policy  # noqa: F401


def test_matrix_default_does_not_start_python(policy, tmp_path, monkeypatch):
    monkeypatch.delenv("IDENTITY_GATE_SCRIPT", raising=False)
    actual_run = subprocess.run
    calls = []

    def observe(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if (
            isinstance(command, (list, tuple))
            and command
            and command[0] == sys.executable
            and any(Path(str(part)).name == "identity_gate.py" for part in command[1:])
        ):
            calls.append(None)
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    result = matrix_message(policy, tmp_path, GREEN["対照 README の文"])
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "pass"
    assert len(calls) == 0


def test_matrix_override_uses_call_time_script(policy, tmp_path, monkeypatch):
    script = tmp_path / "override.py"
    script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "text = Path(args[args.index('--message-file') + 1]).read_text(encoding='utf-8')\n"
        "print('override' if text.startswith('試験\\n\\n') else 'wrong-input')\n"
        "print(os.environ.get('GIT_CONFIG_NOSYSTEM', ''), file=sys.stderr)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("IDENTITY_GATE_SCRIPT", str(script))
    result = matrix_message(policy, tmp_path, GREEN["対照 README の文"])
    assert result.returncode == 7
    assert result.stdout == "override\n"
    assert result.stderr == "1\n"


def test_matrix_missing_override_does_not_fallback(policy, tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_GATE_SCRIPT", str(tmp_path / "missing.py"))
    result = matrix_message(policy, tmp_path, GREEN["対照 README の文"])
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr


@pytest.mark.parametrize("expected", [0, 1])
def test_matrix_matches_cold_cli_and_restores_environment(
    policy, tmp_path, monkeypatch, expected
):
    monkeypatch.delenv("IDENTITY_GATE_SCRIPT", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "outer")
    text = next(iter((GREEN if expected == 0 else RED).values()))
    before = dict(os.environ)
    warm = matrix_message(policy, tmp_path, text)
    assert dict(os.environ) == before
    cold = message(policy, tmp_path, text)
    assert warm.returncode == cold.returncode == expected
    assert warm.stdout == cold.stdout
    assert warm.stderr == cold.stderr


def test_override_does_not_require_default_import(policy, tmp_path, monkeypatch):
    script = tmp_path / "standalone.py"
    script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.setenv("IDENTITY_GATE_SCRIPT", str(script))
    source = Path(__file__).with_name("test_review_3c.py")
    original_spec = importlib.util.spec_from_file_location

    def unavailable_default(name, location, *args, **kwargs):
        if Path(location).name == "identity_gate.py":
            raise ImportError("default entrypoint unavailable")
        return original_spec(name, location, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", unavailable_default)
    helper = runpy.run_path(str(source))
    result = helper["matrix_message"](
        policy, tmp_path, helper["GREEN"]["対照 README の文"]
    )
    assert result.returncode == 7


def test_matrix_input_pairs_are_unique():
    from test_review_3c_extra import GREEN as extra_green, RED as extra_red
    from test_review_3c_human import GREEN as human_green, RED as human_red
    from test_review_3c_route import GREEN as route_green, RED as route_red
    from test_review_3c_tighten import GREEN as tighten_green, RED as tighten_red

    groups = (
        (RED, GREEN),
        (extra_red, extra_green),
        (human_red, human_green),
        (route_red, route_green),
        (tighten_red, tighten_green),
    )
    cases = [
        (text, code)
        for red, green in groups
        for code, table in ((1, red), (0, green))
        for text in table.values()
    ]
    assert len(cases) == len(set(cases))

"""全 CLI が端末やパイプの文字コードに左右されず UTF-8 で出力すること。

Windows の既定コードページ（cp1252 / cp932）でパイプへ日本語を書くと落ちる。CI は
PYTHONUTF8=1 で回しているため、ここでは出力の文字コードを ASCII に絞って再現する。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CLIS = [
    "github_pr_review_snapshot.py",
    "identity_gate.py",
    "install_runtime_skills.py",
    "local_identity_guard.py",
    "preflight_ratchet.py",
    "publish_gate.py",
    "publish_guard_hook.py",
    "readiness_scan.py",
    "runtime_smoke.py",
]


def run_help_with_ascii_stdio(script: str) -> subprocess.CompletedProcess:
    """出力の文字コードを ASCII に絞って `--help` を実行する（他のテストからも使う）。"""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8",)}
    env["PYTHONIOENCODING"] = "ascii"
    env["PYTHONUTF8"] = "0"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--help"],
        capture_output=True,
        env=env,
        timeout=60,
    )
    return proc


@pytest.mark.parametrize("script", CLIS)
def test_help_is_utf8_even_when_stdio_encoding_is_ascii(script):
    assert_utf8_help(script)


def assert_utf8_help(script: str) -> None:
    proc = run_help_with_ascii_stdio(script)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    proc.stdout.decode("utf-8")

"""Run the reusable workflow's install and scan step with local pinned artifacts."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="reusable workflow runs on Ubuntu with bash",
)


@pytest.mark.parametrize("failure", [None, "archive", "binary", "git-scan", "gif-scan"])
@pytest.mark.parametrize("has_base", [False, True])
def test_install_and_scans_use_trusted_pins_and_stop_on_failure(
    tmp_path, failure, has_base
):
    tool = tmp_path / "tool"
    (tool / "scripts").mkdir(parents=True)
    (tool / "references").mkdir()
    shutil.copy2(
        ROOT / "scripts/install_pinned_gitleaks.py",
        tool / "scripts/install_pinned_gitleaks.py",
    )
    binary = (
        b'#!/bin/sh\nprintf "%s\\n" "$*" >> "$SCAN_RECEIPT"\n'
        b'exit "${SCAN_STATUS:-0}"\n'
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("gitleaks")
        member.size = len(binary)
        archive.addfile(member, io.BytesIO(binary))
    payload = buffer.getvalue()
    (tmp_path / "artifact.tgz").write_bytes(payload)
    (tool / "references/gitleaks-pins.json").write_text(
        json.dumps(
            {
                "version": "fixture",
                "archive_sha256": {
                    "linux_x64": (
                        hashlib.sha256(payload).hexdigest()
                        if failure != "archive"
                        else "0" * 64
                    )
                },
                "binary_sha256": {
                    "linux_x64": (
                        hashlib.sha256(binary).hexdigest()
                        if failure != "binary"
                        else "0" * 64
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "install_fixture.py").write_text(
        "import importlib.util, pathlib, sys\n"
        "spec = importlib.util.spec_from_file_location('installer', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module.platform_key = lambda: 'linux_x64'\n"
        "module.fetch = lambda url: pathlib.Path('artifact.tgz').read_bytes()\n"
        "sys.argv = sys.argv[1:]\n"
        "raise SystemExit(module.main())\n",
        encoding="utf-8",
    )
    (tool / "scripts/identity_gate.py").write_text(
        "import json, os, pathlib, shutil, sys\n"
        "pathlib.Path('gif.json').write_text(json.dumps(\n"
        "    {'args': sys.argv[1:], 'gitleaks': shutil.which('gitleaks')}))\n"
        "raise SystemExit(int(os.environ['GIF_STATUS']))\n",
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.mkdir()
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TEMPLATE_DIR=str(tmp_path / "empty-template"),
        TEST_PYTHON=sys.executable,
        RUNNER_TEMP=str(tmp_path / "runner"),
        SCAN_RECEIPT=str(tmp_path / "scan.txt"),
        SCAN_STATUS="1" if failure == "git-scan" else "0",
        GIF_STATUS="2" if failure == "gif-scan" else "0",
    )
    (tmp_path / "empty-template").mkdir()
    subprocess.run(["git", "init", "-q", str(target)], env=env, check=True)
    if has_base:
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "-c",
                "user.name=nexus-ai-2045",
                "-c",
                "user.email=273569186+nexus-ai-2045@users.noreply.github.com",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture base",
            ],
            env=env,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "update-ref",
                "refs/remotes/origin/preflight-event-base",
                "HEAD",
            ],
            env=env,
            check=True,
        )
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    )
    step = next(
        step
        for step in workflow["jobs"]["preflight"]["steps"]
        if step.get("name") == "Run pinned gitleaks"
    )
    shim = (
        'python() { if [ "$1" = tool/scripts/install_pinned_gitleaks.py ]; then\n'
        '  "$TEST_PYTHON" install_fixture.py "$@"\n'
        'else "$TEST_PYTHON" "$@"; fi; }\n'
    )
    result = subprocess.run(
        ["bash", "-c", shim + step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    installed = tmp_path / "runner/gitleaks/gitleaks"
    if failure in {"archive", "binary"}:
        assert result.returncode != 0
        assert not installed.exists()
        assert not (tmp_path / "scan.txt").exists()
        assert not (tmp_path / "gif.json").exists()
        return
    assert installed.read_bytes() == binary
    scan = (tmp_path / "scan.txt").read_text()
    opts = "refs/remotes/origin/preflight-event-base..HEAD" if has_base else "HEAD"
    assert scan == (
        "git --no-banner --redact --config tool/.gitleaks.toml "
        f"--log-opts={opts} target\n"
    )
    if failure == "git-scan":
        assert result.returncode != 0
        assert not (tmp_path / "gif.json").exists()
        return
    gif = json.loads((tmp_path / "gif.json").read_text())
    assert gif["args"] == ["--repo", "target", "--range", opts, "--gitleaks-gif"]
    assert Path(gif["gitleaks"]) == installed
    assert result.returncode == (2 if failure == "gif-scan" else 0)

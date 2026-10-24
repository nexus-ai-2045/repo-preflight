"""Exercise the reusable gate's immutable event binding with real Git history."""

import base64
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import textwrap
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="reusable workflow runs on Ubuntu with bash",
)


def git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "BASH_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    template = tmp_path / "empty-template"
    template.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "master")
    git(origin, "config", "user.name", "nexus_ai")
    git(
        origin,
        "config",
        "user.email",
        "273569186+nexus-ai-2045@users.noreply.github.com",
    )
    marker = origin / "marker.md"

    def commit(text):
        marker.write_text(text, encoding="utf-8")
        git(origin, "add", "marker.md")
        git(origin, "commit", "-qm", text)
        return git(origin, "rev-parse", "HEAD")

    base = commit("base")
    head = commit("event head")
    tip = commit("branch advanced after event")
    git(origin, "checkout", "--orphan", "unrelated")
    git(origin, "commit", "-qm", "unrelated root")
    unrelated = git(origin, "rev-parse", "HEAD")
    # 別の PR の枝とタグ。runner の手元に取れても、この event の判定には使わない
    git(origin, "tag", "other-pr-tag", unrelated)
    git(origin, "checkout", "master")
    target = tmp_path / "target"
    git(tmp_path, "clone", "--quiet", str(origin), str(target))
    git(target, "checkout", "--quiet", "--detach", head)
    tool = tmp_path / "tool"
    (tool / "scripts").mkdir(parents=True)
    (tool / "references").mkdir()
    (tool / "scripts/preflight_ratchet.py").write_text(
        "import json,os,sys\n"
        "assert 'EVENT_FETCH_TOKEN' not in os.environ\n"
        "assert 'event_fetch_token' not in os.environ\n"
        "assert 'auth_header' not in os.environ\n"
        "assert 'GIT_CONFIG_VALUE_0' not in os.environ\n"
        "assert 'GIT_CONFIG_COUNT' not in os.environ\n"
        "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    # push・merge_group でも identity_gate を呼ぶ（敵対的レビュー H6）。呼び出しの引数だけ残す
    (tool / "scripts/identity_gate.py").write_text(
        "import json,os,sys\n"
        "assert 'EVENT_FETCH_TOKEN' not in os.environ\n"
        "assert 'auth_header' not in os.environ\n"
        "open('identity-gate-args.json','w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    # Reuse the actual initial_tree contract; do not reimplement it in the workflow/test.
    shutil.copy2(
        ROOT / "scripts/consistency_gate.py", tool / "scripts/consistency_gate.py"
    )
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    block = text.split("        run: |\n", 1)[1]
    # run: | の本文だけを取り出す（後ろに続く step は含めない）
    block = re.split(r"\n(?= {0,6}\S)", block, maxsplit=1)[0] + "\n"
    script = textwrap.dedent(block)
    shim = tmp_path / "bin"
    shim.mkdir()
    shim_git = shim / "git"
    shim_git.write_text(
        """#!/usr/bin/env python
import base64, json, os, pathlib, sys
assert 'EVENT_FETCH_TOKEN' not in os.environ, 'raw token reached Git process'
assert 'event_fetch_token' not in os.environ, 'shell token was exported'
assert 'auth_header' not in os.environ, 'shell header was exported'
if 'fetch' in sys.argv:
    expected = 'AUTHORIZATION: basic ' + base64.b64encode(
        ('x-access-token:' + os.environ['EXPECTED_FETCH_TOKEN']).encode()).decode()
    assert os.environ.get('GIT_CONFIG_COUNT') == '2', 'fetch auth config absent'
    assert os.environ.get('GIT_CONFIG_KEY_0') == 'http.' + os.environ['GITHUB_SERVER_URL'].rstrip('/') + '/.extraheader'
    assert os.environ.get('GIT_CONFIG_VALUE_0') == expected, 'fetch auth mismatch'
    assert os.environ.get('GIT_CONFIG_KEY_1') == 'credential.helper'
    assert os.environ.get('GIT_CONFIG_VALUE_1') == ''
    assert not any(key in os.environ for key in ('GIT_TRACE', 'GIT_TRACE_CURL', 'GIT_CURL_VERBOSE'))
    pathlib.Path('fetch-auth-receipt.json').write_text(json.dumps({'authenticated': True}))
    if os.environ.get('FETCH_SHIM_REJECT') == 'true':
        raise SystemExit(17)
    if os.environ.get('FETCH_FORCE_TRANSPORT') == 'true':
        sys.argv.insert(sys.argv.index('fetch') + 1, '--refetch')
os.execv(os.environ['REAL_GIT'], [os.environ['REAL_GIT'], *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    shim_git.chmod(0o755)
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "push",
        "EVENT_HEAD_SHA": head,
        "PR_BASE_SHA": "",
        "MERGE_BASE_SHA": "",
        "PUSH_BEFORE_SHA": base,
        "PUSH_FORCED": "false",
        "PUSH_DELETED": "false",
        "BASELINE": ".repo-preflight/ratchet-baseline.json",
        "EVENT_FETCH_TOKEN": "fixture-fetch-credential",
        "event_fetch_token": "ambient-credential",
        "auth_header": "ambient-header",
        "EXPECTED_FETCH_TOKEN": "fixture-fetch-credential",
        "GITHUB_SERVER_URL": "https://github.com",
        "REAL_GIT": shutil.which("git"),
        "PATH": str(shim) + os.pathsep + os.environ["PATH"],
        "GIT_TRACE": "1",
        "GIT_TRACE_CURL": "1",
        "GIT_CURL_VERBOSE": "1",
    }
    return tmp_path, target, base, head, tip, unrelated, script, env


@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group"])
def test_event_base_is_immutable_even_when_master_has_advanced(fixture, event):
    cwd, target, base, head, tip, _, script, env = fixture
    env.update(GITHUB_EVENT_NAME=event, PR_BASE_SHA=base, MERGE_BASE_SHA=base)
    config_hash = hashlib.sha256((target / ".git/config").read_bytes()).hexdigest()
    shell = ["bash", "-ax", "-c", script] if event == "push" else ["bash", "-c", script]
    result = subprocess.run(shell, cwd=cwd, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert args[args.index("--baseline-base-ref") + 1] == base
    ref = args[args.index("--consistency-base-ref") + 1]
    assert ref == "refs/remotes/origin/preflight-event-base"
    assert git(target, "rev-parse", ref) == base
    # master は event のあとに進んでいる（リモート側）。runner の手元からは、event の履歴以外の ref を消す
    assert git(cwd / "origin", "rev-parse", "master") == tip != head
    assert git(target, "for-each-ref", "--format=%(refname)") == ref
    assert "origin/main" not in args and "origin/master" not in args
    assert "--allowed-identities" in args
    # 敵対的レビュー H6: PR だけでなく push・merge_group でも足した範囲の名義と本文を見る
    gate = json.loads((cwd / "identity-gate-args.json").read_text())
    assert (
        gate[gate.index("--range") + 1]
        == "refs/remotes/origin/preflight-event-base..HEAD"
    )
    assert json.loads((cwd / "fetch-auth-receipt.json").read_text()) == {
        "authenticated": True
    }
    assert (
        hashlib.sha256((target / ".git/config").read_bytes()).hexdigest() == config_hash
    )
    encoded = base64.b64encode(
        ("x-access-token:" + env["EVENT_FETCH_TOKEN"]).encode()
    ).decode()
    assert env["EVENT_FETCH_TOKEN"] not in result.stdout + result.stderr
    assert encoded not in result.stdout + result.stderr


def test_authenticated_http_fetch_after_checkout_credentials_were_removed(fixture):
    cwd, target, base, _, _, _, script, env = fixture
    public_data = cwd / "http"
    public_data.mkdir()
    git(
        cwd,
        "clone",
        "--quiet",
        "--bare",
        str(cwd / "origin"),
        str(public_data / "private.git"),
    )
    git(public_data / "private.git", "update-server-info")
    expected = (
        "basic "
        + base64.b64encode(
            ("x-access-token:" + env["EVENT_FETCH_TOKEN"]).encode()
        ).decode()
    )
    observations = {"denied": 0, "accepted": 0}

    class PrivateGit(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(public_data), **kwargs)

        def do_GET(self):
            if self.headers.get("Authorization") != expected:
                observations["denied"] += 1
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                self.end_headers()
                return
            observations["accepted"] += 1
            super().do_GET()

        def log_message(self, *args):
            pass  # No request/header/credential values in test logs.

    server = ThreadingHTTPServer(("127.0.0.1", 0), PrivateGit)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        env["GITHUB_SERVER_URL"] = f"http://127.0.0.1:{server.server_port}"
        # Avoid Git's cached-SHA shortcut only in this transport regression.
        env["FETCH_FORCE_TRANSPORT"] = "true"
        git(
            target,
            "remote",
            "set-url",
            "origin",
            env["GITHUB_SERVER_URL"] + "/private.git",
        )
        before = hashlib.sha256((target / ".git/config").read_bytes()).hexdigest()
        unauthenticated = subprocess.run(
            ["git", "fetch", "--refetch", "--no-tags", "origin", base],
            cwd=target,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
        )
        assert unauthenticated.returncode != 0 and observations["denied"] > 0
        result = subprocess.run(
            ["bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr
        assert observations["accepted"] > 0
        assert (
            git(target, "rev-parse", "refs/remotes/origin/preflight-event-base") == base
        )
        assert (
            hashlib.sha256((target / ".git/config").read_bytes()).hexdigest() == before
        )
        assert env["EVENT_FETCH_TOKEN"] not in result.stdout + result.stderr
        assert expected not in result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "invalid",
        "unknown",
        "disconnected",
        "nonancestor",
        "forced",
        "deleted",
        "head-mismatch",
        "noop",
        "zero-multiple-history",
        "pr-missing",
        "queue-missing",
        "unsupported",
        "token-missing",
        "auth-rejected",
    ],
)
def test_invalid_event_history_never_calls_ratchet(fixture, mode):
    cwd, target, base, head, tip, unrelated, script, env = fixture
    config_hash = hashlib.sha256((target / ".git/config").read_bytes()).hexdigest()
    values = {
        "missing": {"PUSH_BEFORE_SHA": ""},
        "invalid": {"PUSH_BEFORE_SHA": "not-a-commit"},
        "unknown": {"PUSH_BEFORE_SHA": "a" * 40},
        "disconnected": {"PUSH_BEFORE_SHA": unrelated},
        "nonancestor": {"PUSH_BEFORE_SHA": tip},
        "forced": {"PUSH_FORCED": "true"},
        "deleted": {"PUSH_DELETED": "true"},
        "head-mismatch": {"EVENT_HEAD_SHA": base},
        "noop": {"PUSH_BEFORE_SHA": head},
        "zero-multiple-history": {"PUSH_BEFORE_SHA": "0" * 40},
        "pr-missing": {"GITHUB_EVENT_NAME": "pull_request"},
        "queue-missing": {"GITHUB_EVENT_NAME": "merge_group"},
        "unsupported": {"GITHUB_EVENT_NAME": "workflow_dispatch"},
        "token-missing": {"EVENT_FETCH_TOKEN": ""},
        "auth-rejected": {"FETCH_SHIM_REJECT": "true"},
    }
    env.update(values[mode])
    result = subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True
    )
    assert result.returncode != 0
    assert not result.stdout, "the stub ratchet must not execute on invalid binding"
    assert (
        hashlib.sha256((target / ".git/config").read_bytes()).hexdigest() == config_hash
    )
    token = env["EVENT_FETCH_TOKEN"]
    if token:
        encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
        assert token not in result.stdout + result.stderr
        assert encoded not in result.stdout + result.stderr


def test_zero_before_reuses_only_single_complete_root_contract(fixture, tmp_path):
    cwd, target, _, _, _, _, script, env = fixture
    # A separate origin with exactly one root across every ref is the existing
    # initial_tree contract. The preceding negative case rejects multi-history.
    root_origin = tmp_path / "root-origin"
    root_origin.mkdir()
    git(root_origin, "init", "-q", "-b", "master")
    git(
        root_origin,
        "-c",
        "user.name=nexus_ai",
        "-c",
        "user.email=273569186+nexus-ai-2045@users.noreply.github.com",
        "commit",
        "--allow-empty",
        "-qm",
        "single root",
    )
    root_head = git(root_origin, "rev-parse", "HEAD")
    shutil.rmtree(target)
    git(cwd, "clone", "--quiet", str(root_origin), "target")
    env.update(EVENT_HEAD_SHA=root_head, PUSH_BEFORE_SHA="0" * 40)
    result = subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert "--baseline-base-ref" not in args and "--consistency-base-ref" not in args
    assert "--allowed-identities" in args

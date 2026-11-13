"""入口ごとの通過・停止を、終了コードと観測可能なメッセージで固定する特性試験。

挙動を変えない整理の前に契約をピン留めする。値はすべて架空。
本物の個人名・個人メール・実ホームパス・鍵・作業エージェントのセッション URL は書かない。

入口地図（呼び出し側が観測する面）:
- CLI: scripts/identity_gate.py (--public / 非公開)
- CLI: scripts/local_identity_guard.py (config / check / exec)
- CLI: scripts/git_data_privacy_gate.py
- CLI: scripts/readiness_scan.py（終了コードは readiness_scan_report）
- フック: run_git_hook.py -> identity_gate.py --public (--staged / --message-file / --pre-push)
- CI: local_check.py、preflight-gate.yml -> preflight_ratchet.py、identity_gate --public --pr-event
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from path_input_fixture import fixture_path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_GATE = ROOT / "scripts" / "identity_gate.py"
LOCAL_GUARD = ROOT / "scripts" / "local_identity_guard.py"
PRIVACY_GATE = ROOT / "scripts" / "git_data_privacy_gate.py"
REPORT = ROOT / "scripts" / "readiness_scan_report.py"

_J = "".join
PUBLIC_IDENT = "nexus_ai <273569186+nexus-ai-2045@users.noreply.github.com>"
FAKE_HOME = fixture_path("linux", "zz0000", "work")
FAKE_MAIL = _J(["zz0000", "@", "gmail", ".", "com"])
FAKE_SESSION = _J(["Claude", "-Session"]) + ": zz-0000"
FAKE_PERSON = "Fakeo Personaru"
FAKE_PERSON_EMAIL = "fake.person@example.invalid"
PUBLIC_EMAIL = "273569186+nexus-ai-2045@users.noreply.github.com"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GUARD = _load("characterization_local_identity_guard", LOCAL_GUARD)
REPORT_MOD = _load("characterization_readiness_scan_report", REPORT)


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG", "GIT_AUTHOR", "GIT_COMMITTER")) or key in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    template = tmp_path / "empty-template"
    template.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))


def _git(repo: Path, *args: str, name="nexus_ai", email=PUBLIC_EMAIL) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME=name,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=name,
        GIT_COMMITTER_EMAIL=email,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _policy(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "schema": "public-commit-identities/v1",
                "identities": [PUBLIC_IDENT],
                "committer_only": ["GitHub <noreply@github.com>"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _identity_cli(*args: str, cwd=None, stdin: str | None = None, env=None):
    return subprocess.run(
        [sys.executable, str(IDENTITY_GATE), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


# ---- 入口ファイルが揃っていること（地図の構造化） ----


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/identity_gate.py",
        "scripts/local_identity_guard.py",
        "scripts/git_data_privacy_gate.py",
        "scripts/readiness_scan.py",
        "scripts/readiness_scan_report.py",
        "scripts/run_git_hook.py",
        "scripts/manage_git_hooks.py",
        "scripts/local_check.py",
        "scripts/preflight_ratchet.py",
        ".github/workflows/ci.yml",
        ".github/workflows/preflight-gate.yml",
    ],
)
def test_characterization_entry_paths_exist(relative):
    assert (ROOT / relative).is_file()


# ---- identity_gate --public --message-file ----


def test_characterization_public_message_clean_passes(tmp_path):
    policy = _policy(tmp_path)
    message = tmp_path / "msg.txt"
    message.write_text("試験の修正\n", encoding="utf-8")
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(tmp_path),
        "--message-file",
        str(message),
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "pass"' in result.stdout


def test_characterization_public_message_session_line_blocks(tmp_path):
    policy = _policy(tmp_path)
    message = tmp_path / "msg.txt"
    message.write_text(f"試験\n\n{FAKE_SESSION}\n", encoding="utf-8")
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(tmp_path),
        "--message-file",
        str(message),
    )
    assert result.returncode == 1
    assert "セッション行あり" in result.stderr
    assert '"status": "fail"' in result.stdout
    assert "zz-0000" not in _combined(result)


# ---- identity_gate --public --pr-event（日本語見出しの CLI 契約） ----


def test_characterization_pr_event_japanese_headings_pass(tmp_path):
    policy = _policy(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "title": "特性試験を足す",
                    "body": "## 概要\n\n架空の入力だけで契約を固定する。\n",
                }
            }
        ),
        encoding="utf-8",
    )
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(tmp_path),
        "--pr-event",
        str(event),
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "pass"' in result.stdout


@pytest.mark.parametrize(
    "title,body,fragment",
    [
        (
            "Fix docs",
            "## 概要\n\n説明\n",
            "PR題名に日本語がない",
        ),
        (
            "説明を直す",
            "## Summary\n\n説明\n",
            "英語だけの見出しがある",
        ),
        (
            "説明を直す",
            "本文だけ（見出しなし）\n",
            "PR本文に日本語の見出しがない",
        ),
    ],
    ids=["英語題名", "英語見出し", "見出しなし"],
)
def test_characterization_pr_event_japanese_gate_blocks(
    tmp_path, title, body, fragment
):
    policy = _policy(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"title": title, "body": body}}),
        encoding="utf-8",
    )
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(tmp_path),
        "--pr-event",
        str(event),
    )
    assert result.returncode == 1
    assert fragment in result.stderr
    assert '"status": "fail"' in result.stdout


# ---- identity_gate --public --range（差分の形） ----


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "nexus_ai")
    _git(repo, "config", "user.email", PUBLIC_EMAIL)
    (repo / "a.txt").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "初回")
    return repo


def test_characterization_public_range_clean_passes(tmp_path):
    policy = _policy(tmp_path)
    repo = _seed_repo(tmp_path)
    (repo / "b.txt").write_text("連絡先 zz@example.invalid\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "予約ドメイン")
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(repo),
        "--range",
        "HEAD~1..HEAD",
    )
    assert result.returncode == 0, result.stderr


def test_characterization_public_range_personal_mail_blocks(tmp_path):
    policy = _policy(tmp_path)
    repo = _seed_repo(tmp_path)
    (repo / "b.txt").write_text(f"連絡先 {FAKE_MAIL}\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "連絡")
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(repo),
        "--range",
        "HEAD~1..HEAD",
    )
    assert result.returncode == 1
    assert "メールの形" in result.stderr
    assert "zz0000" not in _combined(result)


def test_characterization_public_range_home_path_blocks(tmp_path):
    policy = _policy(tmp_path)
    repo = _seed_repo(tmp_path)
    (repo / "b.txt").write_text(FAKE_HOME + "\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "経路")
    result = _identity_cli(
        "--public",
        "--allowed-identities",
        str(policy),
        "--repo",
        str(repo),
        "--range",
        "HEAD~1..HEAD",
    )
    assert result.returncode == 1
    assert "実ホームパスの形" in result.stderr
    assert "zz0000" not in _combined(result)


# ---- identity_gate 非公開モード（架空の deny パターン） ----


def test_characterization_private_mode_clean_and_denied(tmp_path):
    patterns = {
        "allow_emails": [PUBLIC_EMAIL],
        "allow_email_patterns": ["noreply@github\\.com"],
        "allow_names": ["nexus_ai", "nexus-ai-2045", "GitHub"],
        "deny_patterns": ["kasou-kojin"],
        "allow_path_patterns": [],
    }
    env = dict(os.environ)
    env["PREFLIGHT_PRIVATE_PATTERNS"] = json.dumps(patterns)
    env.pop("PREFLIGHT_PRIVATE_PATTERNS_FILE", None)

    clean = tmp_path / "clean.txt"
    clean.write_text("公開用の説明\n", encoding="utf-8")
    ok = _identity_cli(
        "--repo",
        str(tmp_path),
        "--message-file",
        str(clean),
        env=env,
    )
    assert ok.returncode == 0, ok.stderr
    assert '"status": "pass"' in ok.stdout

    denied = tmp_path / "denied.txt"
    denied.write_text("see kasou-kojin/repo\n", encoding="utf-8")
    blocked = _identity_cli(
        "--repo",
        str(tmp_path),
        "--message-file",
        str(denied),
        env=env,
    )
    assert blocked.returncode == 1
    assert "個人の型に一致" in blocked.stderr
    assert "kasou" not in _combined(blocked).casefold()


# ---- local_identity_guard CLI ----


_ORIGINAL_COLLECT = GUARD.collect_identities


def _fake_identities():
    return _ORIGINAL_COLLECT(
        sources={
            "os_account": ["fakeperson"],
            "full_name": [FAKE_PERSON, "Fakeo", "Personaru"],
            "git_global_email": [FAKE_PERSON_EMAIL, "fake.person"],
            "host_name": ["Fakeo"],
        },
        public_names={"nexus-ai-2045", "nexus_ai"},
    )


def test_characterization_local_identity_guard_check_clean_passes(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "nexus_ai")
    _git(repo, "config", "user.email", PUBLIC_EMAIL)
    (repo / "README.md").write_text("公開用\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    fake = _fake_identities()
    monkeypatch.setattr(GUARD, "collect_identities", lambda **_kw: fake)
    code = GUARD.main(["--repo", str(repo), "check", "--range", "HEAD"])
    assert code == 0


def test_characterization_local_identity_guard_check_personal_blocks(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", FAKE_PERSON)
    _git(repo, "config", "user.email", FAKE_PERSON_EMAIL)
    (repo / "README.md").write_text("公開用\n", encoding="utf-8")
    _git(
        repo,
        "add",
        ".",
        name=FAKE_PERSON,
        email=FAKE_PERSON_EMAIL,
    )
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "init",
        name=FAKE_PERSON,
        email=FAKE_PERSON_EMAIL,
    )
    fake = _fake_identities()
    monkeypatch.setattr(GUARD, "collect_identities", lambda **_kw: fake)
    code = GUARD.main(["--repo", str(repo), "check", "--range", "HEAD"])
    captured = capsys.readouterr()
    assert code == 1
    assert "停止: 公開側に個人名義が出ます" in captured.err
    assert "fake" not in captured.err.casefold()
    assert "personaru" not in captured.err.casefold()


# ---- 終了コード対応（境界の契約） ----


@pytest.mark.parametrize(
    "status,code",
    [("pass", 0), ("blocked", 1), ("tool_error", 2)],
)
def test_characterization_readiness_report_exit_codes(status, code):
    assert REPORT_MOD.report_exit_code({"status": status}) == code


@pytest.mark.parametrize(
    "status,code",
    [
        ("ready_after_confirmation", 0),
        ("blocked", 1),
        ("needs_human_input", 1),
        ("mystery", 2),
    ],
)
def test_characterization_dialogue_exit_codes(status, code):
    assert REPORT_MOD.dialogue_exit_code({"status": status}) == code


def test_characterization_git_data_privacy_gate_cli_exit_mapping():
    """不足引数は tool_error=2。値は出さず reason_codes だけ。"""
    result = subprocess.run(
        [sys.executable, str(PRIVACY_GATE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "tool_error"
    assert "cli_arguments_invalid" in payload["reason_codes"]

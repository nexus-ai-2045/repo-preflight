"""GIF の拡張の本文に入れた鍵形を gitleaks で止める回帰（品質判定 #10 の残り）。

git の差分は GIF を「Binary files differ」としか出さず、gitleaks の既定設定も .gif の名前を外すので、
gitleaks の git の検査だけでは GIF の中の鍵形が届かなかった。鍵形は仮の値で、実行時に組み立てる。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = os.environ.get("IDENTITY_GATE_SCRIPT") or str(ROOT / "scripts/identity_gate.py")
J = "".join
KEY = J(["gh", "p_", "Zz0aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV1"])
ZERO = "0" * 40
POLICY = ""

needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="固定した gitleaks で動く"
)


def _subblocks(payload: bytes, size: int = 255) -> bytes:
    out = b"".join(
        bytes([len(payload[i : i + size])]) + payload[i : i + size]
        for i in range(0, len(payload), size)
    )
    return out + b"\x00"


def gif(comment: bytes | None = None, size: int = 255, app: bytes | None = None):
    """1x1 の GIF を手で組む（Pillow なしでも作れるように）。"""
    data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff"
    if comment is not None:
        data += b"!\xfe" + _subblocks(comment, size)
    if app is not None:
        data += b"!\xff\x0b" + b"FIXTUREAPP1" + _subblocks(app)
    data += b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    return data


@pytest.fixture(autouse=True)
def policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "schema": "public-commit-identities/v1",
                "identities": ["Fixture Bot <fixture@example.invalid>"],
                "committer_only": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "POLICY", str(path), raising=False)
    return path


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Fixture Bot",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture Bot",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(Path(os.environ.get("TMPDIR", "/tmp"))),
    )
    return env


def _git(repo, *args, input=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_env(),
        input=input,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "root")
    return path


def _stage(repo, files):
    for rel, data in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(repo, "add", "-A")


def _commit(repo, files):
    _stage(repo, files)
    _git(repo, "commit", "-qm", "試験")
    return _git(repo, "rev-parse", "HEAD")


def _run(*args, cwd=None, input=None):
    return subprocess.run(
        [sys.executable, GATE, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        input=input,
        env=_env(),
    )


def staged(repo):
    return _run(
        "--public",
        "--allowed-identities",
        POLICY,
        "--repo",
        str(repo),
        "--staged",
        cwd=repo,
    )


def ci_range(repo, rng):
    return _run("--repo", str(repo), "--range", rng, "--gitleaks-gif")


def pre_push(repo, base, head):
    remote = repo.parent / "remote.git"
    if not remote.exists():
        _git(repo.parent, "init", "-q", "--bare", str(remote))
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", f"{base}:refs/heads/main")
        _git(repo, "fetch", "-q", "origin")
    return _run(
        "--public",
        "--allowed-identities",
        POLICY,
        "--repo",
        str(repo),
        "--pre-push",
        "--remote",
        "origin",
        cwd=repo,
        input=f"refs/heads/main {head} refs/heads/main {base}\n",
    )


RED_FORMS = {
    "コメント": {"art/icon.gif": gif(f"token = {KEY}".encode())},
    "コメントのサブブロックの境目で分かれた鍵": {
        "art/icon.gif": gif(f"token = {KEY}".encode(), size=12)
    },
    "アプリ拡張": {"art/icon.gif": gif(app=f"token = {KEY}".encode())},
    "名前を変えた GIF": {"art/icon.bin": gif(f"token = {KEY}".encode())},
    "名前を大文字にした GIF": {"art/ICON.GIF": gif(f"token = {KEY}".encode())},
    "文字だけの .gif": {"art/note.gif": f"token = {KEY}\n".encode()},
    # 同じ中身が GIF でない名前（先に並ぶ a.png）にもあっても、.gif の分を飛ばさない。
    "同じ中身の .png と .gif": {
        "a.png": f"token = {KEY}\n".encode(),
        "art/note.gif": f"token = {KEY}\n".encode(),
    },
}


def test_extension_texts_join_subblocks():
    spec = importlib.util.spec_from_file_location("gate_for_gif", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gate_for_gif"] = module
    spec.loader.exec_module(module)
    texts = module.gif_extension_texts(gif(b"a" * 20 + b"bc", size=7))
    assert b"a" * 20 + b"bc" in texts
    with pytest.raises(ValueError, match="gif_unverified"):
        module.gif_extension_texts(gif(b"abc")[:-6])


@needs_gitleaks
@pytest.mark.parametrize("form", sorted(RED_FORMS))
def test_staged_gif_with_key_is_red(repo, form):
    _stage(repo, RED_FORMS[form])
    result = staged(repo)
    assert result.returncode == 1, (form, result.stderr)
    assert "gitleaks" in result.stderr


@needs_gitleaks
@pytest.mark.parametrize("form", sorted(RED_FORMS))
def test_pushed_gif_with_key_is_red(repo, form):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, RED_FORMS[form])
    result = pre_push(repo, base, head)
    assert result.returncode == 1, (form, result.stderr)
    assert "gitleaks" in result.stderr


@needs_gitleaks
@pytest.mark.parametrize("form", sorted(RED_FORMS))
def test_ci_range_gif_with_key_is_red(repo, form):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, RED_FORMS[form])
    result = ci_range(repo, f"{base}..HEAD")
    assert result.returncode == 1, (form, result.stderr)
    assert "gitleaks" in result.stderr


@needs_gitleaks
def test_clean_gif_passes_everywhere(repo):
    files = {"art/icon.gif": gif(b"ordinary comment"), "art/plain.gif": gif()}
    _stage(repo, files)
    # 新しい画像は別の検査（読めない形のファイル）が人の承認を求めるので、gitleaks の指摘が無いことを見る。
    result = staged(repo)
    assert "gitleaks" not in result.stderr and result.returncode != 2, result.stderr
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-qm", "試験")
    head = _git(repo, "rev-parse", "HEAD")
    result = ci_range(repo, f"{base}..HEAD")
    assert result.returncode == 0, result.stderr
    result = pre_push(repo, base, head)
    assert "gitleaks" not in result.stderr and result.returncode != 2, result.stderr


@needs_gitleaks
def test_broken_gif_is_not_clean(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"art/icon.gif": gif(b"ordinary comment")[:-6]})
    result = ci_range(repo, f"{base}..HEAD")
    assert result.returncode == 2
    assert "GIF" in result.stderr


def test_ci_runs_the_gif_scan_after_gitleaks():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    git_scan = text.index('"$dir/gitleaks" git ')
    gif_scan = text.index("--gitleaks-gif")
    assert git_scan < gif_scan
    line = text[text.rindex("\n", 0, gif_scan) : gif_scan]
    assert 'PATH="$dir:$PATH"' in line


def _gate_module():
    spec = importlib.util.spec_from_file_location("gate_gif_alias", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("reverse", [False, True])
def test_text_blob_alias_is_scanned_regardless_of_order(monkeypatch, reverse):
    module = _gate_module()
    data = f"token = {KEY}".encode()
    monkeypatch.setattr(module, "_git_bytes", lambda *args, **kwargs: data)
    blobs = [("a.png", "same-oid"), ("note.gif", "same-oid")]
    if reverse:
        blobs.reverse()
    assert list(module._gif_inputs("unused", blobs, False)) == [("note.gif", data)]


def test_non_gif_blob_does_not_trigger_stdin(monkeypatch):
    module = _gate_module()
    monkeypatch.setattr(module, "_git_bytes", lambda *args, **kwargs: b"ordinary text")

    def unexpected(data):
        pytest.fail("non-GIF blob reached stdin")

    monkeypatch.setattr(module, "_gitleaks_stdin", unexpected)
    assert module.gif_gitleaks_clean("unused", [("note.txt", "oid")], False)


def test_gif_stdin_failure_blocks(monkeypatch):
    module = _gate_module()
    monkeypatch.setattr(
        module, "_git_bytes", lambda *args, **kwargs: gif(b"payload", size=2)
    )
    seen = []

    def scan(data):
        seen.append(data)
        return False

    monkeypatch.setattr(module, "_gitleaks_stdin", scan)
    assert not module.gif_gitleaks_clean("unused", [("note.bin", "oid")], False)
    assert seen == [b"payload"]


@pytest.mark.parametrize(
    "payload",
    [
        bytes([len(KEY)]) + KEY.encode() + b"\x00",
        b"\x02ab\x02cd\x00",
        b"\x04abcd\x01e\x00",
    ],
)
def test_malformed_graphics_control_extension_is_rejected(payload, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gce_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    raw = gif()
    raw = raw[:19] + b"!\xf9" + payload + raw[19:]
    with pytest.raises(ValueError, match="^gif_unverified$"):
        module.gif_extension_texts(raw)


def test_valid_graphics_control_extension_remains_supported(monkeypatch):
    spec = importlib.util.spec_from_file_location("valid_gce_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    raw = gif()
    assert (
        module.gif_extension_texts(
            raw[:19] + b"!\xf9\x04\x00\x00\x00\x00\x00" + raw[19:]
        )
        == []
    )


def test_gif_ci_entry_rejects_malformed_gce(repo):
    raw = gif()
    bad = raw[:19] + b"!\xf9" + _subblocks(KEY.encode()) + raw[19:]
    _stage(repo, {"image.gif": bad})
    _git(repo, "commit", "-qm", "synthetic malformed control")
    run = subprocess.run(
        [
            sys.executable,
            GATE,
            "--repo",
            str(repo),
            "--range",
            "HEAD~1..HEAD",
            "--gitleaks-gif",
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert run.returncode != 0
    assert KEY not in run.stdout + run.stderr

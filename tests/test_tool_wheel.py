"""保全した検査wheelの同一性と、オフラインのhash強制を確認する。"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
WHEEL = "ai_ratchet_gate-0.1.1-py3-none-any.whl"
EXPECTED = "b3f22ab772699d57906326e42189e0ab7a0ee9e33dcad55a90909ece35106cd7"


def test_sealed_tool_wheel_and_license_are_intact():
    path = ROOT / "vendor" / WHEEL
    assert hashlib.sha256(path.read_bytes()).hexdigest() == EXPECTED
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        original = archive.read("ai_ratchet_gate-0.1.1.dist-info/licenses/LICENSE")
    assert (ROOT / "vendor/ai-ratchet-gate.LICENSE").read_bytes() == original


def test_git_checkout_with_autocrlf_preserves_sealed_artifact_bytes(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "vendor").mkdir()
    for name in [".gitattributes", "vendor/" + WHEEL, "vendor/ai-ratchet-gate.LICENSE"]:
        shutil.copyfile(ROOT / name, repo / name)
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Fixture Author",
        GIT_COMMITTER_NAME="Fixture Author",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, check=True
        )

    git("init", "-q")
    git("config", "core.autocrlf", "true")
    git("add", ".")
    git("commit", "-qm", "Fixture artifact")
    expected_license = (repo / "vendor/ai-ratchet-gate.LICENSE").read_bytes()
    (repo / "vendor" / WHEEL).unlink()
    (repo / "vendor/ai-ratchet-gate.LICENSE").unlink()
    git("checkout", "--", "vendor")
    assert (
        hashlib.sha256((repo / "vendor" / WHEEL).read_bytes()).hexdigest() == EXPECTED
    )
    assert (repo / "vendor/ai-ratchet-gate.LICENSE").read_bytes() == expected_license


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="保全した検査 wheel は開発用で Python 3.11 以上が要る（実行時の検査は 3.9 で動く）",
)
def test_offline_install_rejects_tampered_wheel_and_accepts_original(tmp_path):
    staged = tmp_path / "source"
    (staged / "vendor").mkdir(parents=True)
    shutil.copyfile(ROOT / "requirements-tools.txt", staged / "requirements-tools.txt")
    original = (ROOT / "vendor" / WHEEL).read_bytes()
    wheel = staged / "vendor" / WHEEL
    environment = tmp_path / "venv"
    if sys.version_info < (3, 11) and os.name != "nt" and sys.prefix != sys.base_prefix:
        # 3.9/3.10 は venv の中から venv を作ると元の Python を見失う。実体から作る。
        subprocess.run(
            [os.path.realpath(sys.executable), "-m", "venv", str(environment)],
            check=True,
        )
    else:
        venv.EnvBuilder(with_pip=True).create(environment)
    bindir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    env = dict(
        os.environ,
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONUTF8="1",
    )
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--require-hashes",
        "-r",
        "requirements-tools.txt",
    ]
    wheel.write_bytes(original + b"\n")
    bad = subprocess.run(command, cwd=staged, env=env, capture_output=True, text=True)
    assert bad.returncode != 0
    assert "hash" in (bad.stdout + bad.stderr).lower()
    wheel.write_bytes(original)
    good = subprocess.run(command, cwd=staged, env=env, capture_output=True, text=True)
    assert good.returncode == 0, good.stdout + good.stderr
    help_result = subprocess.run(
        [str(python), "-m", "ai_ratchet_gate", "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "tracked∧ignored" in help_result.stdout

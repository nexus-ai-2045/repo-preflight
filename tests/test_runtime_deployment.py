import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_deployment_installer", ROOT / "scripts/install_runtime_skills.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_checkout_links_are_relative_and_launch_from_another_directory(tmp_path):
    home = tmp_path / "home"
    source = home / "projects/tool"
    for relative in (
        "runtime/claude-code/SKILL.md",
        "runtime/grok/SKILL.md",
        "runtime/shared/run_preflight.py",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_text("# repo-preflight\n")
    (source / "scripts/readiness_scan.py").write_text(
        "import json\nprint(json.dumps({'status': 'pass', 'source': 'fixture'}))\n"
    )
    destination = home / ".claude/skills/repo-preflight"
    result = installer.install_one(
        repo=source, runtime="claude-code", dest=destination, apply=True
    )
    assert result["status"] == "written"
    assert not os.path.isabs(os.readlink(destination / "checkout"))
    assert (destination / "checkout").resolve() == source.resolve()
    env = dict(os.environ)
    env.pop("REPO_PREFLIGHT_ROOT", None)
    ran = subprocess.run(
        [sys.executable, str(destination / "run_preflight.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0
    assert json.loads(ran.stdout) == {"status": "pass", "source": "fixture"}


def test_unmanaged_checkout_directory_is_preserved(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "keep.txt").write_text("unrelated user work\n")
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(FileExistsError):
        installer.link_checkout(checkout, source)
    assert (checkout / "keep.txt").read_text() == "unrelated user work\n"


def test_shared_launcher_exposes_read_only_hook_status(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        REPO_PREFLIGHT_ROOT=str(ROOT),
    )
    subprocess.run(
        ["git", "init", "-q"], cwd=target, env=env, check=True, capture_output=True
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "runtime/shared/run_preflight.py"),
            "--git-hooks",
            "status",
            "--repo",
            str(target),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "not_installed"
    assert not (target / ".git/.repo-preflight-hooks").exists()


def test_invalid_configured_root_does_not_echo_its_value(tmp_path):
    private = str(tmp_path / "unavailable-private-value")
    result = subprocess.run(
        [sys.executable, str(ROOT / "runtime/shared/run_preflight.py"), "--help"],
        env=dict(os.environ, REPO_PREFLIGHT_ROOT=private),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "REPO_PREFLIGHT_ROOT" in result.stderr
    assert private not in result.stdout + result.stderr


def test_missing_optional_entry_does_not_echo_root(tmp_path):
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("# repo-preflight\n")
    (source / "scripts/readiness_scan.py").write_text("# fixture\n")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "runtime/shared/run_preflight.py"),
            "--git-hooks",
            "status",
        ],
        env=dict(os.environ, REPO_PREFLIGHT_ROOT=str(source)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert str(source) not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_install_conflict_preserves_all_existing_adapter_files(tmp_path, kind):
    destination = tmp_path / "skill"
    destination.mkdir()
    before = {}
    for name in ("SKILL.md", "run_preflight.py", "README.md"):
        before[name] = ("old user content " + name).encode()
        (destination / name).write_bytes(before[name])
    checkout = destination / "checkout"
    if kind == "directory":
        checkout.mkdir()
        (checkout / "keep.txt").write_text("user work")
    else:
        checkout.write_text("user work")
    with pytest.raises(FileExistsError):
        installer.install_one(
            repo=ROOT, runtime="claude-code", dest=destination, apply=True
        )
    assert {name: (destination / name).read_bytes() for name in before} == before
    assert (
        (checkout / "keep.txt").read_text() == "user work"
        if kind == "directory"
        else checkout.read_text() == "user work"
    )


def test_batch_install_checks_every_destination_before_writing(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude/skills/repo-preflight"
    claude.mkdir(parents=True)
    (claude / "SKILL.md").write_text("old adapter\n")
    other = home / ".agents/skills/repo-preflight/checkout"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("user work\n")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/install_runtime_skills.py"),
            "--repo",
            str(ROOT),
            "--home",
            str(home),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (claude / "SKILL.md").read_text() == "old adapter\n"
    assert not (claude / "run_preflight.py").exists()


def test_hook_management_never_executes_cwd_root_after_checkout_loss(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    launcher = installed / "run_preflight.py"
    shutil.copyfile(ROOT / "runtime/shared/run_preflight.py", launcher)
    untrusted = tmp_path / "target"
    (untrusted / "scripts").mkdir(parents=True)
    (untrusted / "SKILL.md").write_text("# repo-preflight\n")
    (untrusted / "scripts/readiness_scan.py").write_text("# unrelated source\n")
    (untrusted / "scripts/manage_git_hooks.py").write_text(
        "from pathlib import Path\nPath('unexpected-management').touch()\n"
    )
    env = dict(os.environ)
    env.pop("REPO_PREFLIGHT_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(launcher), "--git-hooks", "install"],
        cwd=untrusted,
        env=env,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (untrusted / "unexpected-management").exists()


@pytest.mark.parametrize("mode", ["relative-env", "relative-marker", "empty-marker"])
def test_hook_management_rejects_cwd_dependent_root_inputs(tmp_path, mode):
    installed = tmp_path / "installed"
    installed.mkdir()
    launcher = installed / "run_preflight.py"
    shutil.copyfile(ROOT / "runtime/shared/run_preflight.py", launcher)
    target = tmp_path / "target"
    (target / "scripts").mkdir(parents=True)
    (target / "SKILL.md").write_text("# repo-preflight\n")
    (target / "scripts/readiness_scan.py").write_text("# fixture\n")
    (target / "scripts/manage_git_hooks.py").write_text(
        "from pathlib import Path\nPath('unexpected-management').touch()\n"
    )
    env = dict(os.environ)
    env.pop("REPO_PREFLIGHT_ROOT", None)
    if mode == "relative-env":
        env["REPO_PREFLIGHT_ROOT"] = "."
    else:
        (installed / "checkout").mkdir()
        (installed / "checkout/ROOT_PATH.txt").write_text(
            ".\n" if mode == "relative-marker" else "\n"
        )
    result = subprocess.run(
        [sys.executable, str(launcher), "--git-hooks", "install"],
        cwd=target,
        env=env,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (target / "unexpected-management").exists()

"""Lifecycle checks use real temporary Git repositories and executable wrappers."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "manage_git_hooks", ROOT / "scripts/manage_git_hooks.py"
)
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    assert result.returncode == 0, "temporary git operation failed"
    return result.stdout


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX hook execution and modes")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "references").mkdir()
    for name in ("manage_git_hooks.py", "run_git_hook.py"):
        shutil.copyfile(ROOT / "scripts" / name, source / "scripts" / name)
    checker = """import json, os, sys
from pathlib import Path
name = Path(__file__).stem
with open("events.jsonl", "a") as stream:
    stream.write(json.dumps({"name": name, "args": sys.argv[1:], "stdin": list(sys.stdin.buffer.read()) if "--pre-push" in sys.argv or name == "push_baseline_guard" else []}) + "\\n")
sys.exit(int(os.environ.get("CHECK_EXIT", "0")))
"""
    for name in ("identity_gate.py", "push_baseline_guard.py"):
        (source / "scripts" / name).write_text(checker)
    for name in (
        "identity_normalization.py",
        "preflight_ratchet.py",
        "local_identity_guard.py",
        "caff_archive.py",
    ):
        (source / "scripts" / name).write_text("# fixture\n")
    for name in ("gitleaks-pins.json", "public-commit-identities.json"):
        (source / "references" / name).write_text("{}\n")
    (source / ".gitleaks.toml").write_text("# fixture\n")
    (source / ".repo-preflight").mkdir()
    (source / ".repo-preflight/identity-fixture-exceptions.txt").write_text(
        "# fixture\n"
    )
    return repo, source


def invoke(repo, source, action, *options):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/manage_git_hooks.py"),
            action,
            "--repo",
            str(repo),
            "--source",
            str(source),
            *options,
        ],
        capture_output=True,
    )


def originals(repo):
    return manager.hooks_directory(repo).parent / "hooks"


def run_hook(repo, hook, args=(), data=None, env=None):
    hooks = manager.hooks_directory(repo)
    return subprocess.run(
        [str(hooks / hook), *args], cwd=repo, input=data, capture_output=True, env=env
    )


def events(repo):
    return [
        json.loads(line) for line in (repo / "events.jsonl").read_text().splitlines()
    ]


def original_hook(path, exit_code=0):
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > original-args\ncat > original-stdin\nexit '
        + str(exit_code)
        + "\n"
    )
    path.chmod(0o751)


def test_plan_status_are_read_only_and_install_update_idempotent(fixture):
    repo, source = fixture
    hooks = manager.hooks_directory(repo)
    before = {p.name: p.read_bytes() for p in originals(repo).iterdir()}
    for action in ("plan", "status"):
        result = invoke(repo, source, action)
        assert result.returncode == 0
        assert json.loads(result.stdout)["status"] == "not_installed"
    assert before == {p.name: p.read_bytes() for p in originals(repo).iterdir()}
    assert not hooks.exists()
    assert invoke(repo, source, "install").returncode == 0
    manifest = (hooks / manager.MANIFEST).read_bytes()
    for action in ("install", "update"):
        result = invoke(repo, source, action)
        assert result.returncode == 0
        assert json.loads(result.stdout)["status"] == "unchanged"
    assert (hooks / manager.MANIFEST).read_bytes() == manifest


def test_stdin_bytes_args_order_failure_and_exact_restore(fixture):
    repo, source = fixture
    target = originals(repo) / "pre-push"
    original_hook(target, 7)
    before = target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
    assert invoke(repo, source, "install").returncode == 0
    data = b"ref\x00\xff value\n\n"
    result = run_hook(repo, "pre-push", ("origin", "unused"), data)
    assert result.returncode == 7
    assert (repo / "original-stdin").read_bytes() == data
    assert (repo / "original-args").read_text() == "origin\nunused\n"
    received = events(repo)
    assert [item["name"] for item in received] == [
        "identity_gate",
        "push_baseline_guard",
    ]
    assert all(bytes(item["stdin"]) == data for item in received)
    assert invoke(repo, source, "uninstall").returncode == 0
    assert (target.read_bytes(), stat.S_IMODE(target.stat().st_mode)) == before
    assert not (target.parent / "commit-msg").exists()
    assert invoke(repo, source, "uninstall").returncode == 0


@pytest.mark.parametrize(
    "hook,args,flag",
    [
        ("pre-commit", (), "--staged"),
        ("commit-msg", ("message file",), "--message-file"),
    ],
)
def test_common_hook_arguments_and_failure_stop_chain(fixture, hook, args, flag):
    repo, source = fixture
    original_hook(originals(repo) / hook)
    assert invoke(repo, source, "install").returncode == 0
    env = dict(os.environ, CHECK_EXIT="9")
    result = run_hook(repo, hook, args, env=env)
    assert result.returncode == 9
    assert flag in events(repo)[0]["args"]
    assert not (repo / "original-args").exists()


@pytest.mark.parametrize(
    "change",
    [
        "wrapper_bytes",
        "wrapper_mode",
        "runner_bytes",
        "manager_mode",
        "manifest",
        "incomplete",
    ],
)
def test_tampering_blocks_status_update_and_uninstall(fixture, change):
    repo, source = fixture
    hooks = manager.hooks_directory(repo)
    original_hook(originals(repo) / "pre-commit")
    assert invoke(repo, source, "install").returncode == 0
    state = hooks
    if change == "wrapper_bytes":
        (hooks / "pre-commit").write_bytes(b"changed")
    elif change == "wrapper_mode":
        (hooks / "pre-commit").chmod(0o644)
    elif change == "runner_bytes":
        (state / "run_git_hook.py").write_bytes(b"changed")
    elif change == "manager_mode":
        (state / "manage_git_hooks.py").chmod(0o644)
    elif change == "manifest":
        (state / manager.MANIFEST).write_bytes(b"{}")
    else:
        (state / "incomplete").write_bytes(b"interrupted")
    for action in ("status", "update", "uninstall"):
        assert invoke(repo, source, action).returncode != 0
    assert run_hook(repo, "commit-msg", ("message",)).returncode != 0


@pytest.mark.parametrize("value", ["custom-hooks", ""])
def test_configured_hooks_path_never_changes(fixture, value):
    repo, source = fixture
    git(repo, "config", "core.hooksPath", value)
    config = (repo / ".git/config").read_bytes()
    for action in ("plan", "install", "status", "update", "uninstall"):
        assert invoke(repo, source, action).returncode != 0
    assert (repo / ".git/config").read_bytes() == config
    assert not manager.hooks_directory(repo).exists()


def test_linked_worktree_shares_common_install(fixture, tmp_path):
    repo, source = fixture
    git(
        repo,
        "-c",
        "user.name=nexus-ai-2045",
        "-c",
        "user.email=nexus-ai-2045@users.noreply.github.com",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    )
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "linked", str(linked))
    assert invoke(linked, source, "install").returncode == 0
    assert manager.hooks_directory(repo) == manager.hooks_directory(linked)
    assert json.loads(invoke(repo, source, "status").stdout)["status"] == "installed"
    git(linked, "hook", "run", "commit-msg", "--", "message")
    assert events(linked)[0]["name"] == "identity_gate"
    git(repo, "hook", "run", "pre-commit")
    assert events(repo)[0]["name"] == "identity_gate"
    assert invoke(repo, source, "uninstall").returncode == 0


def test_source_changed_requires_update_and_then_executes(fixture):
    repo, source = fixture
    assert invoke(repo, source, "install").returncode == 0
    script = source / "scripts/identity_gate.py"
    script.write_text(script.read_text() + "\n# next approved source\n")
    assert invoke(repo, source, "status").returncode != 0
    assert run_hook(repo, "commit-msg", ("message",)).returncode != 0
    assert invoke(repo, source, "update").returncode == 0
    assert run_hook(repo, "commit-msg", ("message",)).returncode == 0


@pytest.mark.parametrize("kind", ["symlink", "directory", "managed"])
def test_existing_conflicts_have_no_partial_install(fixture, kind):
    repo, source = fixture
    hooks = manager.hooks_directory(repo)
    target = originals(repo) / "pre-commit"
    if kind == "symlink":
        target.symlink_to(source / "scripts/identity_gate.py")
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_text(manager.MARKER)
    assert invoke(repo, source, "plan").returncode != 0
    assert invoke(repo, source, "install").returncode != 0
    assert not hooks.exists()


def test_distinct_files_with_identical_bytes_both_run(fixture):
    repo, source = fixture
    original_hook(repo / ".githooks/pre-push")
    shutil.copyfile(repo / ".githooks/pre-push", originals(repo) / "pre-push")
    (originals(repo) / "pre-push").chmod(0o755)
    local = repo / ".githooks/pre-push"
    local.write_text(
        local.read_text().replace(
            "cat > original-stdin", "cat > original-stdin\nprintf x >> calls"
        )
    )
    shutil.copyfile(local, originals(repo) / "pre-push")
    assert invoke(repo, source, "install").returncode == 0
    assert run_hook(repo, "pre-push", ("origin",), b"exact\n").returncode == 0
    assert (repo / "calls").read_bytes() == b"xx"


def test_manifest_and_wrappers_do_not_persist_home(fixture):
    repo, source = fixture
    assert invoke(repo, source, "install").returncode == 0
    hooks = manager.hooks_directory(repo)
    values = [(hooks / hook).read_bytes() for hook in manager.HOOKS]
    values.append((hooks / manager.MANIFEST).read_bytes())
    assert all(os.fsencode(Path.home()) not in value for value in values)
    ref = manager.path_ref(ROOT)
    assert ref["anchor"] in {"home", "absolute"}
    assert manager.resolve_ref(ref).resolve() == ROOT


def test_pending_operation_blocks_all_actions(fixture):
    repo, source = fixture
    lock = manager.hooks_directory(repo).parent / (manager.STATE + ".lock")
    lock.write_bytes(b"pending")
    for action in ("plan", "status", "install", "update", "uninstall"):
        assert invoke(repo, source, action).returncode != 0
    assert lock.read_bytes() == b"pending"


def test_real_common_message_gate_passes_and_blocks(fixture):
    repo, _source = fixture
    assert invoke(repo, ROOT, "install").returncode == 0
    message = repo / "message"
    message.write_text("Public fixture message\n")
    assert run_hook(repo, "commit-msg", ("message",)).returncode == 0
    message.write_text("Co-authored-by: unknown <unknown@example.invalid>\n")
    assert run_hook(repo, "commit-msg", ("message",)).returncode != 0


def test_existing_legacy_template_still_executes_at_original_location(fixture):
    repo, source = fixture
    template = source / "templates/hooks/pre-push"
    template.parent.mkdir(parents=True)
    template.write_text("#!/bin/sh\nexit 81\n")
    target = originals(repo) / "pre-push"
    target.write_bytes(template.read_bytes())
    target.chmod(0o755)
    assert invoke(repo, source, "install").returncode == 0
    assert run_hook(repo, "pre-push", ("origin",), b"\n").returncode == 81
    assert invoke(repo, source, "uninstall").returncode == 0
    assert target.read_bytes() == template.read_bytes()


@pytest.mark.parametrize("kind", ["symlink", "directory", "managed"])
def test_local_hook_conflict_is_detected_before_install(fixture, kind):
    repo, source = fixture
    local = repo / ".githooks/pre-commit"
    local.parent.mkdir()
    if kind == "symlink":
        local.symlink_to(source / "scripts/identity_gate.py")
    elif kind == "directory":
        local.mkdir()
    else:
        local.write_text(manager.MARKER)
    assert invoke(repo, source, "plan").returncode != 0
    assert invoke(repo, source, "install").returncode != 0
    assert not manager.hooks_directory(repo).exists()


def test_windows_refusal_precedes_git_and_writes(tmp_path, monkeypatch):
    before = list(tmp_path.iterdir())
    with monkeypatch.context() as patched:
        patched.setattr(manager.os, "name", "nt")
        for action in ("plan", "status", "install", "update", "uninstall"):
            with pytest.raises(manager.HookError, match="unsupported_platform"):
                manager.manage(action, tmp_path, tmp_path)
    assert list(tmp_path.iterdir()) == before


def test_management_ignores_repository_and_command_config_from_environment(
    fixture, tmp_path, monkeypatch
):
    repo, source = fixture
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    before = (other / ".git/config").read_bytes()
    overrides = {
        "DIR": str(other / ".git"),
        "COMMON_DIR": str(other / ".git"),
        "WORK_TREE": str(other),
        "INDEX_FILE": str(other / ".git/index"),
        "OBJECT_DIRECTORY": str(other / ".git/objects"),
        "ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git/objects"),
        "CONFIG_COUNT": "1",
        "CONFIG_KEY_0": "core.hooksPath",
        "CONFIG_VALUE_0": "wrong",
        "CONFIG_PARAMETERS": "'core.hooksPath'='wrong'",
    }
    for key, value in overrides.items():
        monkeypatch.setenv("GIT_" + key, value)
    for action in ("plan", "install", "status", "update", "uninstall"):
        assert invoke(repo, source, action).returncode == 0
        if action in ("install", "status", "update"):
            assert (repo / ".git" / manager.STATE / manager.MANIFEST).is_file()
        assert not (other / ".git" / manager.STATE).exists()
        assert (other / ".git/config").read_bytes() == before
    assert not (repo / ".git" / manager.STATE).exists()


def test_real_git_preserves_original_location_shell_fallback_and_nonmanaged_hook(
    fixture,
):
    repo, source = fixture
    hooks = originals(repo)
    (hooks / "helper").write_text("printf original > ran-original\n")
    target = hooks / "pre-commit"
    target.write_text('. "$(dirname "$0")/helper"\n')
    target.chmod(0o755)
    post = hooks / "post-commit"
    post.write_text("printf post > ran-post\n")
    post.chmod(0o755)
    inode = target.stat().st_ino
    assert invoke(repo, source, "install").returncode == 0
    git(
        repo,
        "-c",
        "user.name=nexus-ai-2045",
        "-c",
        "user.email=nexus-ai-2045@users.noreply.github.com",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    )
    assert (repo / "ran-original").read_text() == "original"
    assert (repo / "ran-post").read_text() == "post"
    assert invoke(repo, source, "uninstall").returncode == 0
    assert target.stat().st_ino == inode
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--get", "core.hooksPath"]
        ).returncode
        == 1
    )


@pytest.mark.parametrize("scope", ["source", "installed"])
@pytest.mark.parametrize("name", manager.PINNED)
def test_runner_modules_are_verified_before_execution(fixture, scope, name):
    repo, source = fixture
    assert invoke(repo, source, "install").returncode == 0
    directory = (
        source / "scripts" if scope == "source" else manager.hooks_directory(repo)
    )
    (directory / name).write_text(
        "from pathlib import Path\nPath('unexpected-execution').touch()\n"
    )
    assert invoke(repo, source, "status").returncode != 0
    assert run_hook(repo, "pre-commit").returncode != 0
    assert not (repo / "unexpected-execution").exists()


def test_missing_runner_and_moved_source_report_only_fixed_diagnostic(fixture):
    repo, source = fixture
    assert invoke(repo, source, "install").returncode == 0
    source.rename(source.with_name("moved"))
    for missing_runner in (False, True):
        if missing_runner:
            (manager.hooks_directory(repo) / "run_git_hook.py").unlink()
        result = run_hook(repo, "pre-commit")
        assert result.returncode != 0
        assert result.stderr.strip() == b"repo-preflight hook_conflict"


def test_changed_registration_not_unset_or_overwritten(fixture):
    repo, source = fixture
    assert invoke(repo, source, "install").returncode == 0
    git(repo, "config", "--local", "core.hooksPath", "other")
    for action in ("plan", "update", "uninstall"):
        assert invoke(repo, source, action).returncode != 0
        assert git(repo, "config", "--get", "core.hooksPath").strip() == b"other"


def test_new_local_symlink_is_rejected_at_execution(fixture):
    repo, source = fixture
    assert invoke(repo, source, "install").returncode == 0
    (repo / ".githooks").symlink_to(
        manager.hooks_directory(repo), target_is_directory=True
    )
    assert run_hook(repo, "pre-commit").returncode != 0
    assert not (repo / "events.jsonl").exists()


def test_global_hooks_path_and_local_duplicates_are_refused(
    fixture, tmp_path, monkeypatch
):
    repo, source = fixture
    path = tmp_path / "global.gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    git(repo, "config", "--global", "core.hooksPath", "custom")
    for action in ("plan", "install"):
        assert invoke(repo, source, action).returncode != 0
    assert not manager.hooks_directory(repo).exists()
    assert (
        git(repo, "config", "--global", "--get", "core.hooksPath").strip() == b"custom"
    )


def test_real_git_keeps_alternate_index_for_checks(fixture):
    repo, source = fixture
    gate = source / "scripts/identity_gate.py"
    gate.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "Path('seen-index').write_bytes(subprocess.check_output("
        "['git', 'ls-files', '--cached']))\n"
    )
    assert invoke(repo, source, "install").returncode == 0
    (repo / "partial").write_text("staged only in temporary index")
    env = dict(os.environ, GIT_INDEX_FILE=str(repo / ".git/alternate-index"))
    for args in (["add", "partial"], ["hook", "run", "pre-commit"]):
        result = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True)
        assert result.returncode == 0
    assert (repo / "seen-index").read_bytes().strip() == b"partial"
    assert git(repo, "ls-files") == b""


def test_git_expands_home_relative_registration(fixture, monkeypatch):
    repo, source = fixture
    monkeypatch.setenv("HOME", str(repo.parent))
    result = subprocess.run(
        [
            str(Path(sys.executable).resolve()),
            str(ROOT / "scripts/manage_git_hooks.py"),
            "install",
            "--repo",
            str(repo),
            "--source",
            str(source),
        ],
        capture_output=True,
    )
    assert result.returncode == 0
    assert git(repo, "config", "--local", "--get", "core.hooksPath").startswith(b"~/")
    git(repo, "hook", "run", "pre-commit")
    assert events(repo)[0]["name"] == "identity_gate"
    assert invoke(repo, source, "uninstall").returncode == 0


def test_missing_interpreter_writes_fixed_diagnostic(fixture):
    repo, source = fixture
    script = manager.wrapper(
        {
            "python": manager.path_ref(source / "not-installed"),
            "source_files": {
                "scripts/" + n: {"sha256": "0" * 64} for n in manager.PINNED
            },
        },
        "0" * 64,
        "pre-commit",
    )
    target = repo / "wrapper"
    target.write_bytes(script)
    target.chmod(0o755)
    result = subprocess.run([str(target)], cwd=repo, capture_output=True)
    assert result.returncode != 0
    assert result.stderr.strip() == b"repo-preflight hook_conflict"


@pytest.mark.parametrize("change", ["modify", "delete", "add"])
def test_transitive_source_code_is_pinned(fixture, change):
    repo, source = fixture
    dependency = source / "scripts/caff_archive.py"
    assert invoke(repo, source, "install").returncode == 0
    if change == "modify":
        dependency.write_text("raise RuntimeError('must not execute')\n")
    elif change == "delete":
        dependency.unlink()
    else:
        (source / "scripts/another_dependency.py").write_text("# new code\n")
    assert invoke(repo, source, "status").returncode != 0
    result = run_hook(repo, "commit-msg", ("message",))
    assert result.returncode != 0
    assert str(source).encode() not in result.stderr
    assert not (repo / "events.jsonl").exists()


def test_missing_forwarded_hook_uses_fixed_diagnostic(fixture):
    repo, source = fixture
    original = originals(repo) / "post-checkout"
    original_hook(original)
    assert invoke(repo, source, "install").returncode == 0
    original.unlink()
    result = subprocess.run(
        ["git", "hook", "run", "post-checkout"], cwd=repo, capture_output=True
    )
    assert result.returncode != 0
    assert result.stderr.strip() == b"repo-preflight hook_conflict"


def test_replaced_original_hook_directory_uses_fixed_diagnostic(fixture):
    repo, source = fixture
    directory = originals(repo)
    original_hook(directory / "post-checkout")
    assert invoke(repo, source, "install").returncode == 0
    relocated = repo.parent / "saved-hooks"
    directory.rename(relocated)
    directory.symlink_to(relocated, target_is_directory=True)
    result = subprocess.run(
        ["git", "hook", "run", "post-checkout"], cwd=repo, capture_output=True
    )
    assert result.returncode != 0
    assert result.stderr.strip() == b"repo-preflight hook_conflict"
    assert not (repo / "original-args").exists()


def test_subdirectory_does_not_authorize_install_in_parent_repository(fixture):
    repo, source = fixture
    child = repo / "nested"
    child.mkdir()
    assert invoke(child, source, "install").returncode != 0
    assert not manager.hooks_directory(repo).exists()
    result = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
    )
    assert result.returncode == 1


@pytest.mark.parametrize("chain_global", [False, True])
def test_other_worktree_custom_setting_blocks_common_install(
    fixture, tmp_path, monkeypatch, chain_global
):
    repo, source = fixture
    git(repo, "config", "user.name", "nexus_ai")
    git(repo, "config", "user.email", "nexus-ai-2045@users.noreply.github.com")
    git(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "base")
    other = repo.parent / "linked"
    git(repo, "worktree", "add", "-qb", "linked", str(other))
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(other, "config", "--worktree", "core.hooksPath", "custom")
    options = []
    if chain_global:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
        previous = tmp_path.resolve() / "previous"
        previous.mkdir()
        git(repo, "config", "--global", "core.hooksPath", str(previous))
        options = ["--chain-global-hooks"]
    for action in ("plan", "install"):
        assert invoke(repo, source, action, *options).returncode != 0
    assert not manager.hooks_directory(repo).exists()
    assert git(other, "config", "--get", "core.hooksPath").strip() == b"custom"


def test_virtualenv_interpreter_path_is_preserved(fixture):
    import venv

    repo, source = fixture
    environment = repo.parent / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin/python"
    checker = source / "scripts/identity_gate.py"
    checker.write_text(
        "import sys\nfrom pathlib import Path\n"
        "Path('uses-virtualenv').write_text(str(sys.prefix != sys.base_prefix))\n"
    )
    result = subprocess.run(
        [
            str(python),
            str(ROOT / "scripts/manage_git_hooks.py"),
            "install",
            "--repo",
            str(repo),
            "--source",
            str(source),
        ],
        capture_output=True,
    )
    assert result.returncode == 0
    assert run_hook(repo, "commit-msg", ("message",)).returncode == 0
    assert (repo / "uses-virtualenv").read_text() == "True"


def test_adopt_global_preserves_git_hook_and_restores(fixture, tmp_path, monkeypatch):
    repo, source = fixture
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    previous = tmp_path.resolve() / "legacy"
    original_hook(previous / "pre-push", 7)
    original_hook(previous / "post-merge", 8)
    with (previous / "pre-push").open() as f:
        script = f.read().replace("cat >", 'echo "$0" >> locations\ncat >')
    (previous / "pre-push").write_text(script)
    git(repo, "config", "--global", "core.hooksPath", str(previous))
    assert invoke(repo, source, "install", "--chain-global-hooks").returncode == 0
    data = b"ref-update\x00\n\n"
    (repo / "input").write_bytes(data)
    result = subprocess.run(
        ["git", "hook", "run", "--to-stdin=input", "pre-push", "--", "origin", "url"],
        cwd=repo,
        capture_output=True,
    )
    assert result.returncode == 7
    assert (repo / "locations").read_text().splitlines() == [str(previous / "pre-push")]
    assert (repo / "original-args").read_text().splitlines() == ["origin", "url"]
    assert (repo / "original-stdin").read_bytes() == data
    assert run_hook(repo, "post-merge", ["0"]).returncode == 8
    # Git runs receive-side hooks from the Git directory.
    forwarded = manager.hooks_directory(repo) / "post-merge"
    assert (
        subprocess.run([str(forwarded), "0"], cwd=repo / ".git", input=b"").returncode
        == 8
    )
    git(repo, "config", "--global", "core.hooksPath", str(previous) + "-changed")
    assert invoke(repo, source, "update").returncode != 0
    assert run_hook(repo, "pre-push", ["origin", "url"], data).returncode == 1
    git(repo, "config", "--global", "core.hooksPath", str(previous))
    assert invoke(repo, source, "uninstall").returncode == 0
    assert git(repo, "config", "--get", "core.hooksPath").strip() == os.fsencode(
        previous
    )


def test_explicit_gitleaks_is_checked_and_used(fixture, tmp_path, monkeypatch):
    import hashlib

    repo, source = fixture
    binary = tmp_path.resolve() / "tool" / "gitleaks"
    binary.parent.mkdir()
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    pins = {"binary_sha256": {"test": hashlib.sha256(binary.read_bytes()).hexdigest()}}
    (source / "references/gitleaks-pins.json").write_text(json.dumps(pins))
    (source / "scripts/identity_gate.py").write_text(
        "import os, subprocess, sys\n"
        'subprocess.run(["git", "rev-parse", "--git-dir"], check=True)\n'
        'assert os.environ["PATH"] == os.environ["EXPECTED_PATH"]\n'
        'sys.exit(subprocess.call([os.environ["REPO_PREFLIGHT_GITLEAKS"]]))\n'
    )
    sibling = binary.parent / "git"
    sibling.write_text("#!/bin/sh\ntouch wrong-git-called\nexit 93\n")
    sibling.chmod(0o755)
    other = tmp_path / "other"
    other.mkdir()
    (other / "gitleaks").write_bytes(b"#!/bin/sh\nexit 91\n")
    (other / "gitleaks").chmod(0o755)
    monkeypatch.setenv("PATH", str(other) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("EXPECTED_PATH", os.environ["PATH"])
    original = originals(repo) / "pre-commit"
    original.write_text(
        '#!/bin/sh\n[ "$PATH" = "$EXPECTED_PATH" ] || exit 92\n'
        'exec "$REPO_PREFLIGHT_GITLEAKS"\n'
    )
    original.chmod(0o755)
    assert invoke(repo, source, "install", "--gitleaks", str(binary)).returncode == 0
    assert run_hook(repo, "pre-commit").returncode == 0
    assert not (repo / "wrong-git-called").exists()
    binary.write_bytes(b"#!/bin/sh\nexit 0\n# changed\n")
    assert run_hook(repo, "pre-commit").returncode != 0
    assert invoke(repo, source, "update").returncode != 0


def test_missing_adopted_pre_push_cannot_silently_pass(fixture, tmp_path, monkeypatch):
    repo, source = fixture
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "config"))
    previous = tmp_path.resolve() / "global-hooks"
    original_hook(previous / "pre-push", 7)
    git(repo, "config", "--global", "core.hooksPath", str(previous))
    assert invoke(repo, source, "install", "--chain-global-hooks").returncode == 0
    (previous / "pre-push").unlink()
    assert run_hook(repo, "pre-push", ["origin", "url"], b"").returncode != 0
    assert invoke(repo, source, "status").returncode != 0
    assert invoke(repo, source, "update").returncode != 0


def test_status_checks_registered_pin_when_given_another_valid_pin(fixture, tmp_path):
    import hashlib

    repo, source = fixture
    pins = []
    for name in ("registered", "alternate"):
        path = tmp_path.resolve() / name / "gitleaks"
        path.parent.mkdir()
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        pins.append(path)
    registered, alternate = pins
    sha = hashlib.sha256(registered.read_bytes()).hexdigest()
    (source / "references/gitleaks-pins.json").write_text(
        json.dumps({"binary_sha256": {"test": sha}})
    )
    assert (
        invoke(repo, source, "install", "--gitleaks", str(registered)).returncode == 0
    )
    registered.unlink()
    assert invoke(repo, source, "status", "--gitleaks", str(alternate)).returncode != 0
    assert invoke(repo, source, "update", "--gitleaks", str(alternate)).returncode == 0
    assert invoke(repo, source, "status").returncode == 0

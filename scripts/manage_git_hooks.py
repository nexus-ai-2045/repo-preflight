#!/usr/bin/env python3
"""Manage opt-in Git hooks with a repository-local hooks directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

HOOKS = ("pre-commit", "commit-msg", "pre-push")
STATE = ".repo-preflight-hooks"
MANIFEST = "manifest.json"
MARKER = "repo-preflight-managed-hook-v1"
PINNED = ("manage_git_hooks.py", "run_git_hook.py")


class HookError(Exception):
    pass


def git_result(repo, *args):
    # -C alone does not override GIT_DIR or command-scope configuration.
    retained = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in retained
    }
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)


def git(repo, *args):
    result = git_result(repo, *args)
    if result.returncode:
        raise HookError("git_unavailable")
    return os.fsdecode(result.stdout.rstrip(b"\n"))


def hooks_directory(repo):
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    if root != Path(repo).resolve():
        raise HookError("not_repository_root")
    return common_hooks_directory(repo)


def common_hooks_directory(repo):
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common.resolve() / STATE


def registration(hooks):
    ref = path_ref(hooks)
    return ("~/" if ref["anchor"] == "home" else "") + ref["path"]


def config_values(repo, *options):
    result = git_result(repo, "config", *options, "-z", "--get-all", "core.hooksPath")
    if result.returncode == 1 and not result.stdout:
        return []
    if result.returncode or not result.stdout.endswith(b"\x00"):
        raise HookError("hooks_path_conflict")
    return result.stdout[:-1].split(b"\x00")


def inherited_directory(repo):
    values = config_values(repo, "--global", "--includes")
    if len(values) != 1:
        raise HookError("hooks_path_conflict")
    raw = os.fsdecode(values[0])
    path = Path.home() / raw[2:] if raw.startswith("~/") else Path(raw)
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not path.is_dir()
        or not os.access(path, os.R_OK | os.X_OK)
    ):
        raise HookError("hooks_path_conflict")
    return path_ref(path)


def configured(repo, hooks, inherited=None):
    all_values = config_values(repo)
    local = config_values(repo, "--local", "--no-includes")
    if inherited is not None:
        if inherited_directory(repo) != inherited:
            raise HookError("hooks_path_conflict")
        global_values = config_values(repo, "--global", "--includes")
    else:
        global_values = []
    if all_values == global_values and not local:
        return False
    if (
        local == [os.fsencode(registration(hooks))]
        and all_values == global_values + local
    ):
        return True
    raise HookError("hooks_path_conflict")


def validate_worktrees(repo, hooks, inherited=None):
    listing = git_result(repo, "worktree", "list", "--porcelain", "-z")
    if listing.returncode:
        raise HookError("worktree_unavailable")
    locations = [
        Path(os.fsdecode(field[len(b"worktree ") :]))
        for field in listing.stdout.split(b"\x00")
        if field.startswith(b"worktree ")
    ]
    if not locations:
        raise HookError("worktree_unavailable")
    for location in locations:
        if not location.is_dir() or hooks_directory(location) != hooks:
            raise HookError("worktree_unavailable")
        configured(location, hooks, inherited)


def path_ref(path):
    path = Path(os.path.abspath(path))
    try:
        return {"anchor": "home", "path": path.relative_to(Path.home()).as_posix()}
    except ValueError:
        if any(part in ("Users", "home") for part in path.parts):
            raise HookError("unsafe_location")
        return {"anchor": "absolute", "path": str(path)}


def resolve_ref(ref):
    if not isinstance(ref, dict) or set(ref) != {"anchor", "path"}:
        raise HookError("invalid_manifest")
    value = ref["path"]
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        raise HookError("invalid_manifest")
    path = Path(value)
    if ref["anchor"] == "home" and not path.is_absolute() and ".." not in path.parts:
        return Path.home() / path
    if ref["anchor"] == "absolute" and path.is_absolute():
        if any(part in ("Users", "home") for part in path.parts):
            raise HookError("invalid_manifest")
        return path
    raise HookError("invalid_manifest")


def shell_ref(ref):
    resolve_ref(ref)
    if ref["anchor"] == "home":
        return '"$HOME"/' + shlex.quote(ref["path"])
    return shlex.quote(ref["path"])


def regular(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise HookError("non_regular_file")


def fingerprint(path):
    regular(path)
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


def encoded(data):
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


# Isolated Python verifies both installed modules before evaluating either.
# Compile the bytes we hashed; do not import a stale or unverified .pyc.
BOOTSTRAP = """import hashlib, pathlib, sys, types
try:
    root = pathlib.Path(sys.argv[1])
    modules = []
    for name, expected in zip(('manage_git_hooks', 'run_git_hook'), sys.argv[2:4]):
        path = root / (name + '.py')
        if path.is_symlink(): raise ValueError
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected: raise ValueError
        modules.append((name, path, data))
    for name, path, data in modules:
        mod = types.ModuleType(name)
        mod.__file__ = str(path)
        sys.modules[name] = mod
        exec(compile(data, str(path), 'exec'), mod.__dict__)
    sys.exit(mod.main(sys.argv[4:]))
except Exception:
    sys.exit('repo-preflight hook_conflict')
"""


def wrapper(manifest, payload_hash, hook):
    python = shell_ref(manifest["python"])
    hashes = " ".join(
        manifest["source_files"]["scripts/" + n]["sha256"] for n in PINNED
    )
    return (
        "#!/bin/sh\n# " + MARKER + "\n"
        "fail() { echo 'repo-preflight hook_conflict' >&2; exit 1; }\n"
        "root=${0%/*}\n"
        + "py="
        + python
        + "\n"
        + '[ -x "$py" ] && [ -f "$py" ] && [ -f "$root/run_git_hook.py" ] || fail\n'
        + 'exec "$py" -I -S -B -X utf8 -c '
        + shlex.quote(BOOTSTRAP)
        + ' "$root" '
        + hashes
        + ' --manifest "$root/'
        + MANIFEST
        + '" --manifest-sha '
        + payload_hash
        + " --hook "
        + hook
        + ' -- "$@"\n'
    ).encode()


def forwarder(hook):
    return (
        "#!/bin/sh\n# "
        + MARKER
        + "\n"
        + 'parent="${0%/*}/../hooks"\n'
        + '[ -d "$parent" ] && [ ! -L "$parent" ] || '
        + "{ echo 'repo-preflight hook_conflict' >&2; exit 1; }\n"
        + 'original="${0%/*}/../hooks/'
        + hook
        + '"\n'
        + '[ -f "$original" ] && [ -x "$original" ] && [ ! -L "$original" ] || '
        + "{ echo 'repo-preflight hook_conflict' >&2; exit 1; }\n"
        + 'exec "$original" "$@"\n'
    ).encode()


def atomic_write(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix=".hook-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_fingerprints(source):
    required = (
        "scripts/run_git_hook.py",
        "scripts/manage_git_hooks.py",
        "scripts/identity_gate.py",
        "scripts/push_baseline_guard.py",
        "scripts/identity_normalization.py",
        "scripts/preflight_ratchet.py",
        "scripts/local_identity_guard.py",
        "scripts/caff_archive.py",
        "references/gitleaks-pins.json",
        "references/public-commit-identities.json",
        ".gitleaks.toml",
        ".repo-preflight/identity-fixture-exceptions.txt",
    )
    templates = [f"templates/hooks/{hook}" for hook in HOOKS]
    code = [
        p.relative_to(source).as_posix() for p in (source / "scripts").rglob("*.py")
    ]
    return {
        name: fingerprint(source / name)
        for name in (
            *required,
            *code,
            *(n for n in templates if os.path.lexists(source / n)),
        )
    }


def validate_source(manifest):
    source = resolve_ref(manifest["source"])
    if source_fingerprints(source) != manifest["source_files"]:
        raise HookError("source_changed")
    python = resolve_ref(manifest["python"])
    if fingerprint(python.resolve()) != manifest["python_file"]:
        raise HookError("python_changed")
    return source, python


def extra_wrapper(manifest, raw, hook):
    if "global_hooks" in manifest:
        return wrapper(manifest, digest(raw), hook)
    return forwarder(hook)


def managed_files(manifest, raw):
    files = {MANIFEST: (digest(raw), 0o600)}
    for name in PINNED:
        files[name] = (manifest["source_files"]["scripts/" + name]["sha256"], 0o600)
    for hook in HOOKS:
        files[hook] = (digest(wrapper(manifest, digest(raw), hook)), 0o755)
    for hook in manifest["forwarded"]:
        if not valid_extra(hook):
            raise HookError("invalid_manifest")
        script = extra_wrapper(manifest, raw, hook)
        files[hook] = (digest(script), 0o755)
    return files


def read_manifest(hooks, check_source=True):
    if hooks.is_symlink() or not hooks.is_dir():
        raise HookError("invalid_state")
    if os.path.lexists(hooks / "incomplete"):
        raise HookError("incomplete_operation")
    regular(hooks / MANIFEST)
    raw = (hooks / MANIFEST).read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema") != 2:
        raise HookError("invalid_manifest")
    files = managed_files(manifest, raw)
    if {p.name for p in hooks.iterdir()} != set(files):
        raise HookError("unexpected_state_file")
    for name, (sha, mode) in files.items():
        if fingerprint(hooks / name) != {"sha256": sha, "mode": mode}:
            raise HookError("managed_file_changed")
    if check_source:
        validate_source(manifest)
    return manifest


def valid_extra(name):
    return bool(re.fullmatch(r"[a-z][a-z0-9-]*", name)) and name not in (
        *HOOKS,
        "incomplete",
    )


def original_directory(hooks, inherited):
    return resolve_ref(inherited) if inherited is not None else hooks.parent / "hooks"


def hook_inputs(repo, hooks, inherited=None):
    original_dir = original_directory(hooks, inherited)
    local_dir = Path(git(repo, "rev-parse", "--show-toplevel")) / ".githooks"
    for directory in (original_dir, local_dir):
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise HookError("hooks_directory_conflict")
        for name in HOOKS:
            path = directory / name
            if os.path.lexists(path):
                regular(path)
                if MARKER.encode() in path.read_bytes():
                    raise HookError("recursive_hook")
    forwarded = []
    if original_dir.is_dir():
        for path in original_dir.iterdir():
            if valid_extra(path.name) and os.access(path, os.X_OK):
                regular(path)
                if MARKER.encode() in path.read_bytes():
                    raise HookError("recursive_hook")
                forwarded.append(path.name)
    return sorted(forwarded)


def global_files(inherited):
    directory = resolve_ref(inherited)
    return {
        entry.name: fingerprint(entry)
        for entry in directory.iterdir()
        if (entry.name in HOOKS or valid_extra(entry.name))
        and os.access(entry, os.X_OK)
    }


def validate_global_files(manifest):
    if (
        "global_hooks" in manifest
        and global_files(manifest["global_hooks"]) != manifest["global_files"]
    ):
        raise HookError("original_hooks_changed")


def new_manifest(source, forwarded):
    if sys.version_info < (3, 9):
        raise HookError("python_version")
    python = Path(os.path.abspath(sys.executable))
    return {
        "schema": 2,
        "source": path_ref(source),
        "python": path_ref(python),
        "python_file": fingerprint(python.resolve()),
        "source_files": source_fingerprints(source),
        "forwarded": forwarded,
    }


def checked_gitleaks(source, ref):
    binary = resolve_ref(ref)
    if (
        binary.name != "gitleaks"
        or binary.resolve() != binary
        or not os.access(binary, os.X_OK)
    ):
        raise HookError("gitleaks_conflict")
    pins = json.loads((source / "references/gitleaks-pins.json").read_bytes())
    if fingerprint(binary)["sha256"] not in pins["binary_sha256"].values():
        raise HookError("gitleaks_conflict")
    return binary


def _manage(action, repo, source, chain_global_hooks=False, gitleaks=None):
    hooks = hooks_directory(repo)
    present = os.path.lexists(hooks)
    current = (
        read_manifest(hooks, check_source=action not in ("update", "uninstall"))
        if present
        else None
    )
    inherited = current.get("global_hooks") if current else None
    if inherited is None and chain_global_hooks:
        inherited = inherited_directory(repo)
    validate_worktrees(repo, hooks, inherited)
    if configured(repo, hooks, inherited) != present:
        raise HookError("registration_conflict")
    if action == "uninstall":
        if not current:
            return "unchanged"
        atomic_write(hooks / "incomplete", b"uninstall\n", 0o600)
        git(
            repo,
            "config",
            "--local",
            "--fixed-value",
            "--unset",
            "core.hooksPath",
            registration(hooks),
        )
        if configured(repo, hooks, inherited):
            raise HookError("registration_conflict")
        for path in hooks.iterdir():
            if path.name != "incomplete":
                path.unlink()
        (hooks / "incomplete").unlink()
        hooks.rmdir()
        return "uninstalled"
    if current:
        validate_global_files(current)
    forwarded = hook_inputs(repo, hooks, inherited)
    pin = (current or {}).get("gitleaks")
    if action != "status" and gitleaks is not None:
        pin = path_ref(gitleaks)
    if pin is not None:
        pin_source = (
            resolve_ref(current["source"]) if current and action == "status" else source
        )
        checked_gitleaks(pin_source, pin)
    if action == "status":
        if current and current["forwarded"] != forwarded:
            raise HookError("original_hooks_changed")
        return "installed" if present else "not_installed"
    if action == "update" and not present:
        raise HookError("not_installed")
    replacement = new_manifest(source, forwarded)
    if inherited is not None:
        replacement["global_hooks"] = inherited
        replacement["global_files"] = global_files(inherited)
    if pin is not None:
        replacement["gitleaks"] = pin
    if current and action != "update" and current != replacement:
        raise HookError("installation_conflict")
    if action == "plan":
        return "installed" if present else "not_installed"
    if current == replacement:
        return "unchanged"
    if not present:
        hooks.mkdir(mode=0o700)
    atomic_write(hooks / "incomplete", (action + "\n").encode(), 0o600)
    raw = encoded(replacement)
    if current:
        for name in set(current["forwarded"]) - set(forwarded):
            (hooks / name).unlink()
    for name in forwarded:
        atomic_write(hooks / name, extra_wrapper(replacement, raw, name), 0o755)
    for name in PINNED:
        atomic_write(hooks / name, (source / "scripts" / name).read_bytes(), 0o600)
    for hook in HOOKS:
        atomic_write(hooks / hook, wrapper(replacement, digest(raw), hook), 0o755)
    atomic_write(hooks / MANIFEST, raw, 0o600)
    if not present:
        git(repo, "config", "--local", "--add", "core.hooksPath", registration(hooks))
    if not configured(repo, hooks, inherited):
        raise HookError("registration_conflict")
    (hooks / "incomplete").unlink()
    read_manifest(hooks)
    return "updated" if present else "installed"


def manage(action, repo, source, chain_global_hooks=False, gitleaks=None):
    if os.name == "nt":
        raise HookError("unsupported_platform")
    hooks = hooks_directory(repo)
    lock = hooks.parent / (STATE + ".lock")
    if action in ("plan", "status"):
        if lock.exists() or lock.is_symlink():
            raise HookError("operation_in_progress")
        return _manage(action, repo, source, chain_global_hooks, gitleaks)
    try:
        descriptor = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise HookError("operation_in_progress") from None
    try:
        os.close(descriptor)
        return _manage(action, repo, source, chain_global_hooks, gitleaks)
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("plan", "install", "status", "update", "uninstall")
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--chain-global-hooks", action="store_true")
    parser.add_argument("--gitleaks", type=Path)
    args = parser.parse_args(argv)
    try:
        status = manage(
            args.action,
            args.repo,
            args.source.resolve(),
            args.chain_global_hooks,
            args.gitleaks,
        )
        print(json.dumps({"status": status, "hooks": list(HOOKS)}))
        return 0
    except HookError as exc:
        code = str(exc)
    except (OSError, ValueError, KeyError, TypeError):
        code = "invalid_state"
    print(json.dumps({"status": "conflict", "reason": code}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

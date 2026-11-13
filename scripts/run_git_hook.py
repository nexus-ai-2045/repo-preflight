#!/usr/bin/env python3
"""Run validated common checks and preserved repository hooks."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import subprocess
import sys

from manage_git_hooks import (
    HOOKS,
    MANIFEST,
    MARKER,
    configured,
    checked_gitleaks,
    original_directory,
    hook_inputs,
    HookError,
    digest,
    common_hooks_directory,
    hooks_directory,
    validate_global_files,
    read_manifest,
    regular,
    validate_source,
)


def run(manifest_path, expected_sha, hook, args, data):
    regular(manifest_path)
    if manifest_path.name != MANIFEST:
        raise HookError("invalid_manifest")
    if digest(manifest_path.read_bytes()) != expected_sha:
        raise HookError("manifest_changed")
    hooks = (hooks_directory if hook in HOOKS else common_hooks_directory)(Path.cwd())
    if manifest_path.parent.resolve() != hooks:
        raise HookError("wrong_repository")
    manifest = read_manifest(hooks, check_source=False)
    inherited = manifest.get("global_hooks")
    if not configured(Path.cwd(), hooks, inherited):
        raise HookError("wrong_repository")
    source, python = validate_source(manifest)
    validate_global_files(manifest)
    original = original_directory(hooks, inherited) / hook
    if hook not in HOOKS:
        if not inherited or hook not in manifest["forwarded"]:
            raise HookError("invalid_hook")
        return execute_hook(original, args, data)
    if hook_inputs(Path.cwd(), hooks, inherited) != manifest["forwarded"]:
        raise HookError("original_hooks_changed")
    env = None
    if "gitleaks" in manifest:
        binary = checked_gitleaks(source, manifest["gitleaks"])
        env = dict(os.environ, REPO_PREFLIGHT_GITLEAKS=str(binary))
    command = [
        str(python),
        "-E",
        "-s",
        "-X",
        "utf8",
        str(source / "scripts" / "identity_gate.py"),
        "--public",
        "--repo",
        ".",
    ]
    if hook == "pre-commit":
        command += ["--staged"]
    elif hook == "commit-msg":
        if not args:
            raise HookError("missing_message")
        command += ["--message-file", args[0]]
    else:
        if not args:
            raise HookError("missing_remote")
        command += ["--pre-push", "--remote", args[0]]
    commands = [command]
    if hook == "pre-push":
        commands.append(
            [
                str(python),
                "-E",
                "-s",
                "-X",
                "utf8",
                str(source / "scripts" / "push_baseline_guard.py"),
                "--repo",
                ".",
                "--remote",
                args[0],
            ]
        )
    for command in commands:
        result = subprocess.run(command, input=data, env=env)
        if result.returncode:
            return result.returncode if result.returncode > 0 else 1
    local = Path.cwd() / ".githooks" / hook
    chained = []
    if os.access(original, os.X_OK):
        chained.append(original)
    legacy = source / "templates" / "hooks" / hook
    handles_local = (
        chained and legacy.is_file() and original.read_bytes() == legacy.read_bytes()
    )
    if (
        os.access(local, os.X_OK)
        and not handles_local
        and not any(local.samefile(p) for p in chained)
    ):
        chained.append(local)
    for path in chained:
        code = execute_hook(path, args, data, env=env)
        if code:
            return code
    return 0


def execute_hook(path, args, data, env=None):
    regular(path)
    if MARKER.encode() in path.read_bytes():
        raise HookError("recursive_hook")
    try:
        result = subprocess.run([str(path), *args], input=data, env=env)
    except OSError as exc:
        if exc.errno != errno.ENOEXEC:
            raise
        result = subprocess.run(["/bin/sh", str(path), *args], input=data, env=env)
    return result.returncode if result.returncode >= 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    args = options.args[1:] if options.args[:1] == ["--"] else options.args
    data = sys.stdin.buffer.read() if options.hook == "pre-push" else None
    try:
        return run(options.manifest, options.manifest_sha, options.hook, args, data)
    except (HookError, OSError, ValueError, KeyError, TypeError):
        print("repo-preflight hook_conflict", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ("pre-commit", "commit-msg", "pre-push")


def run(argv, cwd, env, data=None):
    result = subprocess.run(
        argv, cwd=cwd, env=env, input=data, capture_output=True, timeout=120
    )
    if result.returncode:
        raise RuntimeError("benchmark_command_failed")
    return result.stdout


def fixture(base, commits):
    repo = base / "target"
    remote = base / "remote.git"
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    run(["git", "init", "-q", "-b", "main", str(repo)], base, env)
    run(["git", "init", "-q", "--bare", str(remote)], base, env)
    git = lambda *args: run(["git", *args], repo, env)
    for key, value in (
        ("user.name", "nexus_ai"),
        ("user.email", "273569186+nexus-ai-2045@users.noreply.github.com"),
        ("commit.gpgsign", "false"),
    ):
        git("config", key, value)
    baseline = repo / ".repo-preflight/ratchet-baseline.json"
    baseline.parent.mkdir()
    template = json.loads((ROOT / ".repo-preflight/ratchet-baseline.json").read_text())
    template["counts"] = {key: 0 for key in template["counts"]}
    baseline.write_text(json.dumps(template), encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "Benchmark base")
    before = git("rev-parse", "HEAD").strip().decode("ascii")
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "main")
    for index in range(commits):
        (repo / "sample.txt").write_text(
            f"revision {index}\n" + "ordinary sample data\n" * 256, encoding="utf-8"
        )
        git("add", "sample.txt")
        git("commit", "-q", "-m", f"Benchmark revision {index}")
    head = git("rev-parse", "HEAD").strip().decode("ascii")
    (repo / "staged.txt").write_text("ordinary staged data\n" * 256, encoding="utf-8")
    git("add", "staged.txt")
    message = base / "message.txt"
    message.write_text("Benchmark message\n", encoding="utf-8")
    stdin = f"refs/heads/main {head} refs/heads/main {before}\n".encode("ascii")
    return repo, env, message, stdin


def measure(roots, samples, commits, modes):
    source_hashes = {label: source_hash(root) for label, root in roots.items()}
    observations = {label: {hook: [] for hook in HOOKS} for label in roots}
    with tempfile.TemporaryDirectory(prefix="preflight-hook-benchmark-") as folder:
        repo, env, message, stdin = fixture(Path(folder), commits)
        for trial in range(samples):
            labels = list(roots)
            if trial % 2:
                labels.reverse()
            for label in labels:
                tool = roots[label]
                scoped_env = dict(env, REPO_PREFLIGHT_HOME=str(tool))
                manager = [sys.executable, str(tool / "scripts/manage_git_hooks.py")]
                managed = modes[label] == "managed"
                if managed:
                    run(manager + ["install", "--repo", str(repo)], repo, scoped_env)
                try:
                    for hook in HOOKS:
                        directory = tool / "templates/hooks"
                        if managed:
                            directory = Path(
                                os.fsdecode(
                                    run(
                                        ["git", "rev-parse", "--git-path", "hooks"],
                                        repo,
                                        scoped_env,
                                    ).strip()
                                )
                            )
                            if not directory.is_absolute():
                                directory = repo / directory
                        argv = ["sh", str(directory / hook)]
                        data = None
                        if hook == "commit-msg":
                            argv.append(str(message))
                        elif hook == "pre-push":
                            argv.extend(["origin", "unused-local-destination"])
                            data = stdin
                        started = time.perf_counter()
                        run(argv, repo, scoped_env, data)
                        observations[label][hook].append(time.perf_counter() - started)
                finally:
                    if managed:
                        run(
                            manager + ["uninstall", "--repo", str(repo)],
                            repo,
                            scoped_env,
                        )
    if source_hashes != {label: source_hash(root) for label, root in roots.items()}:
        raise RuntimeError("benchmark_source_changed")
    return observations, source_hashes


def source_hash(root):
    digest = hashlib.sha256()
    files = sorted(
        [*(root / "scripts").rglob("*.py"), *root.glob("references/*.json")]
        + list((root / "templates/hooks").glob("*"))
        + [root / ".gitleaks.toml"]
        + [
            root / ".repo-preflight/identity-fixture-exceptions.txt",
            root / ".repo-preflight/ratchet-baseline.json",
        ]
    )
    for file in files:
        if file.is_file():
            digest.update(file.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure real Git hook templates against a synthetic local repository."
    )
    parser.add_argument("--tool-root", type=Path, default=ROOT)
    parser.add_argument("--compare-root", type=Path)
    parser.add_argument("--mode", choices=("templates", "managed"), default="templates")
    parser.add_argument(
        "--compare-mode", choices=("templates", "managed"), default="templates"
    )
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--commits", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.samples < 1 or args.commits < 1:
        parser.error("samples and commits must be positive")
    roots = {"primary": args.tool_root.resolve()}
    modes = {"primary": args.mode}
    if args.compare_root is not None:
        roots["comparison"] = args.compare_root.resolve()
        modes["comparison"] = args.compare_mode
    try:
        observations, source_hashes = measure(roots, args.samples, args.commits, modes)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"status": "tool_error", "reason": "benchmark_failed"}))
        return 2
    report = {
        "schema": "repo-preflight.hook-benchmark/v1",
        "status": "pass",
        "driver_python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "fixture": {
            "outgoing_commits": args.commits,
            "staged_bytes": len("ordinary staged data\n".encode()) * 256,
            "network": False,
        },
        "scope": "Real hook commands including subprocess startup and pinned gitleaks; local remote only. Installation is outside timing.",
        "non_guarantees": [
            "hosted CI latency",
            "network push latency",
            "media workloads",
        ],
        "errors": 0,
        "results": {},
    }
    for label, by_hook in observations.items():
        report["results"][label] = {
            "mode": modes[label],
            "source_sha256": source_hashes[label],
            "templates_sha256": {
                name: hashlib.sha256(
                    (roots[label] / "templates/hooks" / name).read_bytes()
                ).hexdigest()
                for name in HOOKS
            },
            "hooks": {
                name: {
                    "seconds": values,
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                }
                for name, values in by_hook.items()
            },
        }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

REQUIRED = ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "PUBLIC_READY.md")
DEPENDENCY_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
)
PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[/\\]Us" + r"ers[/\\][^/\\\s]+"),
    re.compile(r"/Us" + r"ers/[^/\s]+"),
    re.compile(r"/ho" + r"me/[^/\s]+"),
)


def run(repo: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(args, cwd=repo, text=True, capture_output=True, shell=False)
    return result.returncode, result.stdout.strip()


def redact_remote(remote: str) -> str | None:
    if not remote:
        return None
    if "://" not in remote:
        sanitized = remote.split("?", 1)[0].split("#", 1)[0]
        scp_match = re.match(r"^(?:[^@/:]+@)?([^/:]+):(.+)$", sanitized)
        if scp_match and ("@" in sanitized or "." in scp_match.group(1)):
            return f"{scp_match.group(1)}:{scp_match.group(2)}"
        return sanitized
    parts = urlsplit(remote)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def text_has(patterns: tuple[re.Pattern, ...], data: bytes) -> bool:
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        decoded_text = unquote(text)
        if any(
            pattern.search(candidate)
            for candidate in (text, decoded_text)
            for pattern in patterns
        ):
            return True
    return False


def sanitized_evidence_label(label: str) -> str:
    data = label.encode("utf-8", errors="surrogatepass")
    if text_has(SECRET_PATTERNS + PATH_PATTERNS, data):
        return "<redacted-path>"
    return label


def effective_identity(value: str) -> str:
    return re.sub(r"\s+\d+\s+[+-]\d{4}$", "", value).strip()


def history_hits(repo: Path) -> tuple[list[str], list[str]]:
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=repo,
        capture_output=True,
    )
    if objects.returncode:
        raise RuntimeError("git_history_inventory_failed")
    inventory_by_id: dict[str, str] = {}
    for line in objects.stdout.splitlines():
        object_id, _, raw_name = line.partition(b" ")
        try:
            oid = object_id.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if oid:
            inventory_by_id.setdefault(
                oid, raw_name.decode("utf-8", errors="surrogateescape")
            )
    inventory = list(inventory_by_id.items())
    object_ids = [object_id for object_id, _ in inventory]
    check = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=repo,
        input="\n".join(object_ids) + "\n",
        text=True,
        capture_output=True,
    )
    if check.returncode:
        raise RuntimeError("git_history_inventory_failed")
    metadata_lines = check.stdout.splitlines()
    if len(metadata_lines) != len(inventory):
        raise RuntimeError("git_history_inventory_failed")
    eligible: list[tuple[str, str, int]] = []
    for source, metadata in zip(inventory, metadata_lines, strict=True):
        object_id, name = source
        fields = metadata.split()
        if len(fields) != 3 or fields[0] != object_id:
            raise RuntimeError("git_history_inventory_failed")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if fields[1] not in {"blob", "commit", "tree", "tag"} or size < 0:
            raise RuntimeError("git_history_inventory_failed")
        if fields[1] == "blob" and size <= 2_000_000:
            eligible.append((object_id, name, size))
    secret_hits: set[str] = set()
    path_hits: set[str] = set()
    eligible.reverse()
    while eligible:
        chunk: list[tuple[str, str, int]] = []
        chunk_bytes = 0
        while eligible and (not chunk or chunk_bytes + eligible[-1][2] <= 16_000_000):
            item = eligible.pop()
            chunk.append(item)
            chunk_bytes += item[2]
        batch = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=repo,
            input=("\n".join(object_id for object_id, _, _ in chunk) + "\n").encode(
                "ascii"
            ),
            capture_output=True,
        )
        if batch.returncode:
            raise RuntimeError("git_history_inventory_failed")
        cursor = 0
        try:
            for object_id, name, expected_size in chunk:
                line_end = batch.stdout.index(b"\n", cursor)
                header = batch.stdout[cursor:line_end].decode("ascii").split()
                if len(header) != 3 or header[0] != object_id or header[1] != "blob":
                    raise RuntimeError("git_history_inventory_failed")
                size = int(header[2])
                if size != expected_size:
                    raise RuntimeError("git_history_inventory_failed")
                start = line_end + 1
                end = start + size
                if end >= len(batch.stdout) or batch.stdout[end : end + 1] != b"\n":
                    raise RuntimeError("git_history_inventory_failed")
                data = batch.stdout[start:end]
                cursor = end + 1
                label = f"history:{sanitized_evidence_label(name or object_id[:12])}"
                if text_has(SECRET_PATTERNS, data):
                    secret_hits.add(label)
                if text_has(PATH_PATTERNS, data):
                    path_hits.add(label)
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if cursor != len(batch.stdout):
            raise RuntimeError("git_history_inventory_failed")
    return sorted(secret_hits), sorted(path_hits)


def working_tree_files(repo: Path) -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--stage"],
        cwd=repo,
        capture_output=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
    )
    if tracked.returncode or untracked.returncode:
        raise RuntimeError("git_worktree_inventory_failed")
    paths: list[Path] = []
    for entry in tracked.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_name = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError("git_worktree_inventory_failed")
        mode = fields[0]
        if mode in {b"120000", b"160000"}:
            continue
        if not mode.startswith(b"100"):
            raise RuntimeError("git_worktree_inventory_failed")
        paths.append(repo / raw_name.decode("utf-8", errors="surrogateescape"))
    for raw_name in untracked.stdout.split(b"\0"):
        if raw_name:
            paths.append(repo / raw_name.decode("utf-8", errors="surrogateescape"))
    return paths


def deleted_working_tree_files(repo: Path) -> set[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--deleted"], cwd=repo, capture_output=True
    )
    if result.returncode:
        raise RuntimeError("git_worktree_inventory_failed")
    return {
        repo / name.decode("utf-8", errors="surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    }


def scan(repo: Path, expected_identity: str | None = None) -> dict:
    repo = repo.resolve()
    if not repo.is_dir():
        return {"status": "tool_error", "issues": ["not_git_repository"]}
    git_code, top = run(repo, "git", "rev-parse", "--show-toplevel")
    if git_code:
        return {"status": "tool_error", "issues": ["not_git_repository"]}
    repo = Path(top).resolve()
    probes = {
        "head": run(repo, "git", "rev-parse", "HEAD"),
        "dirty": run(repo, "git", "status", "--porcelain"),
        "identities": run(repo, "git", "log", "--format=%an <%ae>|%cn <%ce>", "--all"),
        "effective_author": run(repo, "git", "var", "GIT_AUTHOR_IDENT"),
        "effective_committer": run(repo, "git", "var", "GIT_COMMITTER_IDENT"),
    }
    if any(code for code, _ in probes.values()):
        return {
            "status": "tool_error",
            "repo": str(repo),
            "issues": ["git_probe_failed"],
        }
    head = probes["head"][1]
    dirty = probes["dirty"][1]
    identities = probes["identities"][1]
    _, remote = run(repo, "git", "remote", "get-url", "origin")
    missing = [name for name in REQUIRED if not (repo / name).is_file()]
    secret_hits: list[str] = []
    path_hits: list[str] = []
    try:
        paths = working_tree_files(repo)
        deleted_paths = deleted_working_tree_files(repo)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": str(repo),
            "issues": ["git_worktree_inventory_failed"],
        }
    for path in paths:
        if path in deleted_paths:
            continue
        rel = path.relative_to(repo).as_posix()
        if not path.is_file():
            return {
                "status": "tool_error",
                "repo": str(repo),
                "issues": [f"worktree_file_unreadable:{rel}"],
            }
        try:
            data = path.read_bytes()
        except OSError:
            return {
                "status": "tool_error",
                "repo": str(repo),
                "issues": [f"worktree_file_unreadable:{rel}"],
            }
        if text_has(SECRET_PATTERNS, data):
            secret_hits.append(sanitized_evidence_label(rel))
        if text_has(PATH_PATTERNS, data):
            path_hits.append(sanitized_evidence_label(rel))
    try:
        history_secret_hits, history_path_hits = history_hits(repo)
        secret_hits.extend(history_secret_hits)
        path_hits.extend(history_path_hits)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": str(repo),
            "issues": ["git_history_inventory_failed"],
        }
    identity_lines = {line for line in identities.splitlines() if line}
    identity_mismatches = {
        line
        for line in identity_lines
        if expected_identity
        and (
            line.split("|")[0] != expected_identity
            or line.split("|")[-1] != expected_identity
        )
    }
    effective_identities = {
        effective_identity(probes["effective_author"][1]),
        effective_identity(probes["effective_committer"][1]),
    }
    effective_mismatches = {
        identity
        for identity in effective_identities
        if expected_identity and identity != expected_identity
    }
    workflows = sorted(
        list((repo / ".github" / "workflows").glob("*.y*ml"))
        if (repo / ".github" / "workflows").is_dir()
        else []
    )
    invalid_workflows: list[str] = []
    for workflow in workflows:
        try:
            workflow_text = workflow.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            invalid_workflows.append(workflow.relative_to(repo).as_posix())
            continue
        if not re.search(r"(?m)^jobs\s*:", workflow_text):
            invalid_workflows.append(workflow.relative_to(repo).as_posix())
    dependency_files = sorted(
        name for name in DEPENDENCY_FILES if (repo / name).is_file()
    )
    checks = {
        "clean_worktree": {"status": "pass" if not dirty else "fail"},
        "required_documents": {
            "status": "pass" if not missing else "fail",
            "missing": missing,
        },
        "secret_scan": {
            "status": "pass" if not secret_hits else "fail",
            "files": secret_hits,
        },
        "personal_path_scan": {
            "status": "pass" if not path_hits else "fail",
            "files": path_hits,
        },
        "commit_identity": {
            "status": (
                "pass"
                if not identity_mismatches and not effective_mismatches
                else "fail"
            ),
            "policy": "expected_identity" if expected_identity else "not_configured",
            "identity_count": len(identity_lines),
            "mismatch_count": len(identity_mismatches),
            "effective_mismatch_count": len(effective_mismatches),
        },
        "dependency_configuration": {
            "status": "pass" if dependency_files else "not_applicable",
            "files": dependency_files,
        },
        "dependency_vulnerability_audit": {
            "status": "unknown" if dependency_files else "not_applicable",
            "reason": "requires_ecosystem_specific_current_audit",
        },
        "ci_configuration": {
            "status": (
                "fail" if invalid_workflows else "pass" if workflows else "unknown"
            ),
            "workflow_count": len(workflows),
            "invalid_files": invalid_workflows,
        },
        "ci_runtime_result": {
            "status": "unknown" if workflows else "not_applicable",
            "reason": "requires_current_remote_ci_evidence",
        },
        "human_visual_review": {
            "status": "unknown",
            "reason": "explicit_human_review_required",
        },
        "origin": {
            "status": "pass" if remote else "unknown",
            "url": redact_remote(remote),
        },
    }
    automated_check_names = {
        "clean_worktree",
        "required_documents",
        "secret_scan",
        "personal_path_scan",
        "commit_identity",
        "dependency_configuration",
        "ci_configuration",
        "origin",
    }
    blocking = any(checks[name]["status"] == "fail" for name in automated_check_names)
    unknown = any(checks[name]["status"] == "unknown" for name in automated_check_names)
    return {
        "status": "blocked" if blocking or unknown else "pass",
        "publication_decision": "blocked_human_review_required",
        "repo": top,
        "head": head,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--expected-identity",
        help='Expected Git author and committer identity, for example "Example <dev@example.com>"',
    )
    args = parser.parse_args()
    report = scan(args.repo, expected_identity=args.expected_identity)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return (
        0
        if report["status"] == "pass"
        else 2 if report["status"] == "tool_error" else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

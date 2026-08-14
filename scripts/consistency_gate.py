from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


SCHEMA = "repo-preflight.consistency/v1"
CONFIG_NAME = ".repo-preflight-consistency.json"
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _matches(path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)
        for pattern in patterns
    )


def _repo_path(repo: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel:
        raise ValueError("invalid_consistency_config")
    path = (repo / rel).resolve()
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise ValueError("invalid_consistency_config") from exc
    return path


def _load_config(repo: Path) -> dict | None:
    path = repo / CONFIG_NAME
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid_consistency_config")
    if (
        not isinstance(config, dict)
        or config.get("schema") != SCHEMA
        or config.get("mode") not in {"shadow", "enforce"}
    ):
        raise ValueError("invalid_consistency_config")
    for key in ("impact_map", "generated_artifacts"):
        if key in config and not isinstance(config[key], list):
            raise ValueError("invalid_consistency_config")
    return config


def _tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True)
    if result.returncode:
        raise RuntimeError("git_consistency_inventory_failed")
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _changed_files(repo: Path, base_ref: str | None) -> list[str]:
    if not base_ref:
        return []
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", base_ref, "--"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git_consistency_diff_failed")
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _markdown_findings(repo: Path, files: list[str], patterns: list[str]) -> list[str]:
    findings: list[str] = []
    for rel in files:
        if not _matches(rel, patterns):
            continue
        path = repo / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(f"markdown_unreadable:{rel}")
            continue
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            parts = urlsplit(target)
            if parts.scheme or parts.netloc or target.startswith(("#", "mailto:")):
                continue
            decoded = unquote(parts.path)
            if not decoded:
                continue
            resolved = (path.parent / decoded).resolve()
            try:
                resolved.relative_to(repo)
            except ValueError:
                findings.append(f"markdown_link_outside_repo:{rel}:{decoded}")
                continue
            if not resolved.exists():
                findings.append(f"markdown_link_missing:{rel}:{decoded}")
    return findings


def _readme_findings(repo: Path, contracts: dict) -> list[str]:
    findings: list[str] = []
    readme = repo / "README.md"
    try:
        body = readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["readme_unreadable"]
    required_paths = contracts.get("required_paths", [])
    commands = contracts.get("commands", [])
    if not isinstance(required_paths, list) or not isinstance(commands, list):
        raise ValueError("invalid_consistency_config")
    for rel in required_paths:
        if not _repo_path(repo, rel).exists():
            findings.append(f"readme_path_missing:{rel}")
    for command in commands:
        if not isinstance(command, dict) or not isinstance(command.get("text"), str):
            raise ValueError("invalid_consistency_config")
        text = command["text"]
        if text not in body:
            findings.append(f"readme_command_missing:{text}")
        paths = command.get("paths", [])
        if not isinstance(paths, list):
            raise ValueError("invalid_consistency_config")
        for rel in paths:
            if not _repo_path(repo, rel).exists():
                finding = f"readme_path_missing:{rel}"
                if finding not in findings:
                    findings.append(finding)
    return findings


def _impact_results(
    changed: list[str], rules: list[dict]
) -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    findings: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("invalid_consistency_config")
        change = rule.get("change")
        required = rule.get("requires_any")
        if (
            not isinstance(change, list)
            or not change
            or not isinstance(required, list)
            or not required
        ):
            raise ValueError("invalid_consistency_config")
        affected = any(_matches(path, change) for path in changed)
        satisfied = any(_matches(path, required) for path in changed)
        status = "pass" if not affected or satisfied else "fail"
        results.append({"change": change, "requires_any": required, "status": status})
        if status == "fail":
            findings.append(f"related_docs_update_missing:{change[0]}")
    return results, findings


def _artifact_findings(
    repo: Path, artifacts: list[dict], changed: list[str]
) -> list[str]:
    findings: list[str] = []
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", "")))
        ):
            raise ValueError("invalid_consistency_config")
        rel = artifact["path"]
        path = _repo_path(repo, rel)
        sources = artifact.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError("invalid_consistency_config")
        if any(_matches(item, sources) for item in changed) and not any(
            item in {rel, CONFIG_NAME} for item in changed
        ):
            findings.append(f"generated_artifact_update_missing:{rel}")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            findings.append(f"generated_artifact_missing:{rel}")
            continue
        if digest != artifact["sha256"]:
            findings.append(f"generated_artifact_drift:{rel}")
    return findings


def check(repo: Path, *, base_ref: str | None = None) -> dict:
    repo = repo.resolve()
    try:
        config = _load_config(repo)
        if config is None:
            return {"status": "not_configured", "mode": None, "findings": []}
        files = _tracked_files(repo)
        changed = _changed_files(repo, base_ref)
        findings: list[str] = []
        markdown = config.get("markdown", {})
        if not isinstance(markdown, dict):
            raise ValueError("invalid_consistency_config")
        patterns = markdown.get("include", ["README.md", "docs/**/*.md"])
        if not isinstance(patterns, list):
            raise ValueError("invalid_consistency_config")
        findings.extend(_markdown_findings(repo, files, patterns))
        contracts = config.get("readme_contracts", {})
        if not isinstance(contracts, dict):
            raise ValueError("invalid_consistency_config")
        findings.extend(_readme_findings(repo, contracts))
        impact, impact_findings = _impact_results(changed, config.get("impact_map", []))
        findings.extend(impact_findings)
        findings.extend(
            _artifact_findings(repo, config.get("generated_artifacts", []), changed)
        )
        findings = sorted(set(findings))
        mode = config["mode"]
        status = (
            "pass"
            if not findings
            else ("fail" if mode == "enforce" else "shadow_findings")
        )
        return {
            "status": status,
            "mode": mode,
            "finding_count": len(findings),
            "findings": findings,
            "impact": impact,
        }
    except ValueError:
        return {
            "status": "tool_error",
            "mode": None,
            "findings": ["invalid_consistency_config"],
        }
    except RuntimeError as exc:
        return {"status": "tool_error", "mode": None, "findings": [str(exc)]}

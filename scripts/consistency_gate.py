from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

SCHEMA = "repo-preflight.consistency/v1"

# readiness_scan と同じ。GIT_DIR 等で別 repository の判定を返さない。
_GIT_REPO_OVERRIDE_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_INDEX_FILE",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_isolation_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in _GIT_REPO_OVERRIDE_VARS:
        env.pop(key, None)
    return env


def run_subprocess(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args")
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
        kwargs["env"] = git_isolation_env(kwargs.get("env"))
    return subprocess.run(*args, **kwargs)


CONFIG_NAME = ".repo-preflight-consistency.json"
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
ACTION_USES_RE = re.compile(
    rb"^(\s*(?:-\s+)?uses:\s+)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)@([0-9a-f]{40})((?:[ \t]+#.*)?[ \t]*)$"
)
YAML_KEY_RE = re.compile(rb"^( *)(- )?([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
CAPABILITY_ROUTES = (
    {
        "id": "readability-template",
        "patterns": ["README.md", "docs/**"],
        "plugins": ["template-creator"],
        "reason": "文書構造を再利用可能な参照付きテンプレートとして整える候補",
    },
    {
        "id": "product-design-audit",
        "patterns": ["README.md", "docs/**", "web/**", "ui/**"],
        "plugins": ["product-design"],
        "reason": "情報設計・視認性・アクセシビリティを具体的な画面または文書に結び付けて確認する候補",
    },
    {
        "id": "security-guidance",
        "patterns": ["SECURITY.md", "**/auth/**", "**/security/**", "*.pem"],
        "plugins": ["security-guidance"],
        "reason": "認証・秘密情報・security境界の追加レビュー候補",
    },
    {
        "id": "creative-production",
        "patterns": ["assets/**", "images/**", "**/*.png", "**/*.svg", "**/*.mp4"],
        "plugins": ["creative-production"],
        "reason": "視覚素材の一貫性と制作工程を確認する候補",
    },
    {
        "id": "openai-developers",
        "patterns": ["**/openai/**", "**/*openai*", "**/*chatgpt*"],
        "plugins": ["openai-developers"],
        "reason": "OpenAI API・Agents SDK・ChatGPT Appの公式実装ガイダンス確認候補",
    },
    {
        "id": "github-workflow",
        "patterns": [".github/**"],
        "plugins": ["github"],
        "reason": "GitHub Actions・PR表示・repository設定の確認候補",
    },
)


def _matches(path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(path, pattern)
        or PurePosixPath(path).match(pattern)
        or (
            "**/" in pattern
            and (
                fnmatch.fnmatchcase(path, pattern.replace("**/", "", 1))
                or PurePosixPath(path).match(pattern.replace("**/", "", 1))
            )
        )
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
    _validate_config(config)
    return config


def _string_list(value: object, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(type(item) is str and bool(item) for item in value)
    )


def _exact_keys(value: object, allowed: set[str], required: set[str] = set()) -> bool:
    return isinstance(value, dict) and required <= set(value) and set(value) <= allowed


def _validate_config(config: object) -> None:
    top_keys = {
        "$schema",
        "schema",
        "mode",
        "markdown",
        "readme_contracts",
        "impact_map",
        "generated_artifacts",
        "ratchet",
    }
    if not _exact_keys(config, top_keys, {"schema", "mode"}):
        raise ValueError("invalid_consistency_config")
    assert isinstance(config, dict)
    if config["schema"] != SCHEMA or config["mode"] not in {
        "shadow",
        "ratchet",
        "enforce",
    }:
        raise ValueError("invalid_consistency_config")
    if "$schema" in config and type(config["$schema"]) is not str:
        raise ValueError("invalid_consistency_config")
    markdown = config.get("markdown", {})
    if not _exact_keys(markdown, {"include"}) or not _string_list(
        markdown.get("include", [])
    ):
        raise ValueError("invalid_consistency_config")
    contracts = config.get("readme_contracts", {})
    if not _exact_keys(contracts, {"required_paths", "commands"}) or not _string_list(
        contracts.get("required_paths", [])
    ):
        raise ValueError("invalid_consistency_config")
    commands = contracts.get("commands", [])
    if not isinstance(commands, list):
        raise ValueError("invalid_consistency_config")
    for command in commands:
        if (
            not _exact_keys(command, {"text", "paths"}, {"text"})
            or type(command["text"]) is not str
            or not command["text"]
            or not _string_list(command.get("paths", []))
        ):
            raise ValueError("invalid_consistency_config")
    impact_map = config.get("impact_map", [])
    if not isinstance(impact_map, list):
        raise ValueError("invalid_consistency_config")
    for rule in impact_map:
        if (
            not _exact_keys(
                rule,
                {"change", "requires_any", "allow_github_action_ref_updates"},
                {"change", "requires_any"},
            )
            or not _string_list(rule["change"], nonempty=True)
            or not _string_list(rule["requires_any"], nonempty=True)
            or (
                "allow_github_action_ref_updates" in rule
                and type(rule["allow_github_action_ref_updates"]) is not bool
            )
        ):
            raise ValueError("invalid_consistency_config")
    artifacts = config.get("generated_artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError("invalid_consistency_config")
    for artifact in artifacts:
        if (
            not _exact_keys(artifact, {"path", "sources", "sha256"}, {"path", "sha256"})
            or type(artifact["path"]) is not str
            or not artifact["path"]
            or not _string_list(artifact.get("sources", []))
            or type(artifact["sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        ):
            raise ValueError("invalid_consistency_config")
    if "ratchet" in config:
        ratchet = config["ratchet"]
        if not _exact_keys(ratchet, {"baseline"}, {"baseline"}) or not _string_list(
            ratchet["baseline"]
        ):
            raise ValueError("invalid_consistency_config")
        baseline = ratchet["baseline"]
        if len(baseline) != len(set(baseline)):
            raise ValueError("invalid_consistency_config")


def _capability_recommendations(changed: list[str]) -> list[dict]:
    recommendations: list[dict] = []
    for route in CAPABILITY_ROUTES:
        evidence = sorted(path for path in changed if _matches(path, route["patterns"]))
        if evidence:
            recommendations.append(
                {
                    "id": route["id"],
                    "plugins": route["plugins"],
                    "reason": route["reason"],
                    "evidence_paths": evidence,
                    "execution": "human_review_required",
                }
            )
    return recommendations


def _ratchet_result(config: dict, findings: list[str]) -> dict:
    ratchet = config.get("ratchet", {})
    if not isinstance(ratchet, dict):
        raise ValueError("invalid_consistency_config")
    baseline = ratchet.get("baseline", [])
    if not isinstance(baseline, list) or any(
        not isinstance(item, str) or not item for item in baseline
    ):
        raise ValueError("invalid_consistency_config")
    baseline_set = set(baseline)
    finding_set = set(findings)
    return {
        "accepted": sorted(finding_set & baseline_set),
        "new": sorted(finding_set - baseline_set),
        "resolved": sorted(baseline_set - finding_set),
    }


def _tracked_files(repo: Path) -> list[str]:
    result = run_subprocess(
        ["git", "ls-files", "-z", "--stage"], cwd=repo, capture_output=True
    )
    if result.returncode:
        raise RuntimeError("git_consistency_inventory_failed")
    files: list[str] = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_name = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != b"0":
            raise RuntimeError("git_consistency_inventory_failed")
        mode = fields[0]
        if mode in {b"120000", b"160000"}:
            continue
        if not mode.startswith(b"100"):
            raise RuntimeError("git_consistency_inventory_failed")
        files.append(raw_name.decode("utf-8", errors="surrogateescape"))
    return files


def _changed_files(repo: Path, base_ref: str | None) -> list[str]:
    if not base_ref:
        return []
    result = run_subprocess(
        ["git", "diff", "--name-status", "-z", base_ref, "--"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git_consistency_diff_failed")
    fields = [item for item in result.stdout.split(b"\0") if item]
    changed: list[str] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor].decode("ascii", errors="strict")
        cursor += 1
        if status[:1] in {"R", "C"}:
            if cursor + 1 >= len(fields):
                raise RuntimeError("git_consistency_diff_failed")
            changed.extend(
                [
                    fields[cursor].decode("utf-8", errors="surrogateescape"),
                    fields[cursor + 1].decode("utf-8", errors="surrogateescape"),
                ]
            )
            cursor += 2
        else:
            if cursor >= len(fields):
                raise RuntimeError("git_consistency_diff_failed")
            changed.append(fields[cursor].decode("utf-8", errors="surrogateescape"))
            cursor += 1
    return changed


def _markdown_findings(
    repo: Path, files: list[str], tracked_files: set[str], patterns: list[str]
) -> list[str]:
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
            target_rel = resolved.relative_to(repo).as_posix()
            if not resolved.exists() or target_rel not in tracked_files:
                findings.append(f"markdown_link_missing:{rel}:{decoded}")
    return findings


def _readme_findings(repo: Path, contracts: dict, tracked_files: set[str]) -> list[str]:
    findings: list[str] = []
    readme = repo / "README.md"
    if "README.md" not in tracked_files:
        return ["readme_unreadable:README.md"]
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
    for index, command in enumerate(commands, start=1):
        if not isinstance(command, dict) or not isinstance(command.get("text"), str):
            raise ValueError("invalid_consistency_config")
        text = command["text"]
        if text not in body:
            command_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            findings.append(f"readme_command_missing:command-{index}-{command_id}")
        paths = command.get("paths", [])
        if not isinstance(paths, list):
            raise ValueError("invalid_consistency_config")
        for rel in paths:
            if not _repo_path(repo, rel).exists():
                finding = f"readme_path_missing:{rel}"
                if finding not in findings:
                    findings.append(finding)
    return findings


def _action_uses_lines(source: bytes) -> set[int] | None:
    lines_in_source = source.splitlines()
    lines: set[int] = set()
    jobs_seen = False
    job_indent: int | None = None
    job_child_indent: int | None = None
    steps_indent: int | None = None
    step_indent: int | None = None
    block_scalar_indent: int | None = None
    for line_number, raw_line in enumerate(lines_in_source):
        if b"\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            return None
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(b" "))
        if block_scalar_indent is not None:
            if indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        match = YAML_KEY_RE.fullmatch(raw_line)
        if match is None:
            if jobs_seen and indent > 0:
                return None
            continue
        has_dash = match.group(2) is not None
        key = match.group(3)
        value = (match.group(4) or b"").strip()
        # YAML properties can precede a scalar or collection indicator. This
        # scanner intentionally does not interpret them; fail closed instead.
        if value.startswith((b"&", b"!")):
            return None
        if value.startswith(b"'") and not value.endswith(b"'"):
            return None
        if value.startswith(b'"'):
            backslashes = 0
            for byte in reversed(value[:-1]):
                if byte != ord("\\"):
                    break
                backslashes += 1
            if not value.endswith(b'"') or backslashes % 2:
                return None
        # Indentation does not describe ownership inside YAML flow collections.
        # Refuse the exemption instead of risking that a nested non-Action
        # ``uses`` key is mistaken for a job or step key.
        if value.startswith((b"{", b"[")):
            return None
        if value.startswith((b"|", b">")):
            block_scalar_indent = indent
        if indent == 0:
            jobs_seen = key == b"jobs" and not has_dash
            job_indent = None
            job_child_indent = None
            steps_indent = None
            step_indent = None
            continue
        if not jobs_seen:
            continue
        if job_indent is None:
            if has_dash:
                return None
            job_indent = indent
            continue
        if indent < job_indent:
            return None
        if indent == job_indent:
            if has_dash:
                return None
            job_child_indent = None
            steps_indent = None
            step_indent = None
            continue
        if job_child_indent is None:
            if has_dash:
                return None
            job_child_indent = indent
        if indent == job_child_indent:
            steps_indent = indent if key == b"steps" and not has_dash else None
            step_indent = None
            if key == b"uses" and not has_dash:
                lines.add(line_number)
            continue
        if steps_indent is None:
            continue
        if has_dash:
            if step_indent is None:
                step_indent = indent
            elif indent != step_indent:
                return None
            if key == b"uses":
                lines.add(line_number)
            continue
        if step_indent is not None and indent == step_indent + 2 and key == b"uses":
            lines.add(line_number)
    return lines


def _github_action_ref_update_only(repo: Path, base_ref: str, rel: str) -> bool:
    workflow_path = Path(rel)
    if (
        workflow_path.parent.as_posix() != ".github/workflows"
        or workflow_path.suffix
        not in {
            ".yml",
            ".yaml",
        }
    ):
        return False
    base_entry = run_subprocess(
        ["git", "ls-tree", "-z", base_ref, "--", rel],
        cwd=repo,
        capture_output=True,
    )
    index_entry = run_subprocess(
        ["git", "ls-files", "-s", "-z", "--", rel],
        cwd=repo,
        capture_output=True,
    )
    working_diff = run_subprocess(
        ["git", "diff", "--raw", "-z", base_ref, "--", rel],
        cwd=repo,
        capture_output=True,
    )
    if base_entry.returncode or index_entry.returncode or working_diff.returncode:
        return False
    base_mode = base_entry.stdout.partition(b" ")[0]
    index_mode = index_entry.stdout.partition(b" ")[0]
    diff_fields = [field for field in working_diff.stdout.split(b"\0") if field]
    if len(diff_fields) != 2:
        return False
    metadata = diff_fields[0].split()
    if (
        len(metadata) != 5
        or not metadata[0].startswith(b":")
        or metadata[4] != b"M"
        or diff_fields[1] != rel.encode("utf-8", errors="surrogateescape")
    ):
        return False
    old_mode = metadata[0][1:]
    current_mode = metadata[1]
    if (
        base_mode not in {b"100644", b"100755"}
        or old_mode != base_mode
        or index_mode != base_mode
        or current_mode != base_mode
        or (repo / rel).is_symlink()
    ):
        return False
    result = run_subprocess(
        ["git", "show", f"{base_ref}:{rel}"], cwd=repo, capture_output=True
    )
    if result.returncode:
        return False
    try:
        current = (repo / rel).read_bytes()
    except OSError:
        return False
    before_lines = result.stdout.splitlines()
    after_lines = current.splitlines()
    if len(before_lines) != len(after_lines):
        return False
    before_action_lines = _action_uses_lines(result.stdout)
    after_action_lines = _action_uses_lines(current)
    if before_action_lines is None or after_action_lines is None:
        return False
    found_update = False
    for line_number, (before, after) in enumerate(zip(before_lines, after_lines)):
        if before == after:
            continue
        if (
            line_number not in before_action_lines
            or line_number not in after_action_lines
        ):
            return False
        before_match = ACTION_USES_RE.fullmatch(before)
        after_match = ACTION_USES_RE.fullmatch(after)
        if (
            before_match is None
            or after_match is None
            or before_match.group(1) != after_match.group(1)
            or before_match.group(2) != after_match.group(2)
        ):
            return False
        found_update = True
    return found_update


def _impact_results(
    repo: Path, base_ref: str, changed: list[str], rules: list[dict]
) -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    findings: list[str] = []
    for index, rule in enumerate(rules, start=1):
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
        affected_paths = [path for path in changed if _matches(path, change)]
        action_ref_update_only = (
            bool(affected_paths)
            and bool(rule.get("allow_github_action_ref_updates", False))
            and all(
                _github_action_ref_update_only(repo, base_ref, path)
                for path in affected_paths
            )
        )
        affected = bool(affected_paths) and not action_ref_update_only
        satisfied = any(_matches(path, required) for path in changed)
        status = "pass" if not affected or satisfied else "fail"
        results.append(
            {
                "change": change,
                "requires_any": required,
                "status": status,
                "github_action_ref_update_only": action_ref_update_only,
            }
        )
        if status == "fail":
            findings.append(f"related_docs_update_missing:impact-{index}")
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
        if any(_matches(item, sources) for item in changed) and rel not in changed:
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
        impact_rules = config.get("impact_map", [])
        artifacts = config.get("generated_artifacts", [])
        change_sensitive = bool(impact_rules) or any(
            artifact.get("sources") for artifact in artifacts
        )
        if change_sensitive and base_ref is None:
            return {
                "status": "tool_error",
                "mode": None,
                "findings": ["change_sensitive_scope_unavailable"],
            }
        findings: list[str] = []
        tracked_files = set(files)
        markdown = config.get("markdown", {})
        if not isinstance(markdown, dict):
            raise ValueError("invalid_consistency_config")
        patterns = markdown.get("include", ["README.md", "docs/**/*.md"])
        if not isinstance(patterns, list):
            raise ValueError("invalid_consistency_config")
        if "readme_contracts" not in config:
            patterns = [
                pattern for pattern in patterns if not _matches("README.md", [pattern])
            ]
        findings.extend(_markdown_findings(repo, files, tracked_files, patterns))
        if "readme_contracts" in config:
            contracts = config["readme_contracts"]
            findings.extend(_readme_findings(repo, contracts, tracked_files))
        impact, impact_findings = _impact_results(
            repo, base_ref or "", changed, impact_rules
        )
        findings.extend(impact_findings)
        findings.extend(_artifact_findings(repo, artifacts, changed))
        findings = sorted(set(findings))
        mode = config["mode"]
        ratchet = _ratchet_result(config, findings) if mode == "ratchet" else None
        unreadable_markdown = any(
            finding.startswith(("markdown_unreadable:", "readme_unreadable:"))
            for finding in findings
        )
        if unreadable_markdown:
            status = "tool_error"
        elif mode == "ratchet":
            status = "fail" if ratchet["new"] or ratchet["resolved"] else "pass"
        else:
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
            "ratchet": ratchet,
            "capability_recommendations": _capability_recommendations(changed),
        }
    except (TypeError, ValueError):
        return {
            "status": "tool_error",
            "mode": None,
            "findings": ["invalid_consistency_config"],
        }
    except RuntimeError as exc:
        return {"status": "tool_error", "mode": None, "findings": [str(exc)]}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="README・docs・実装・テストの宣言済み整合性を検査する"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--require-config",
        action="store_true",
        help="設定なしを成功扱いにせず、CIの自己防衛に使う",
    )
    parser.add_argument(
        "--require-mode",
        choices=("shadow", "ratchet", "enforce"),
        help="設定modeの弱体化を拒否する",
    )
    args = parser.parse_args()
    report = check(args.repo, base_ref=args.base_ref)
    if args.require_config and report["status"] == "not_configured":
        report = {
            "status": "tool_error",
            "mode": None,
            "findings": ["required_consistency_config_missing"],
        }
    elif args.require_mode and report.get("mode") != args.require_mode:
        findings = sorted(
            set(report.get("findings", [])) | {"required_consistency_mode_mismatch"}
        )
        report = {
            **report,
            "status": "tool_error",
            "finding_count": len(findings),
            "findings": findings,
        }
    print(
        # Windows CIの非UTF-8 pipeでもJSON契約を壊さない。
        json.dumps(report, ensure_ascii=True, indent=2)
        if args.json
        else report["status"]
    )
    return 0 if report["status"] in {"pass", "not_configured"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

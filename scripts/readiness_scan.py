from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, TextIO
from urllib.parse import unquote, urlsplit, urlunsplit

SCHEMA = "repo-preflight.scan/v3"

# CLIが担当する範囲の境界。対話・非対話のどちらでも同じ文言を出す。
GUARANTEES = (
    "ローカルGitの現在treeと履歴を読み取り専用で検査する",
    "選択した検査scopeでsecret候補・個人path・作者名義を機械判定し、repo全体modeでは必須文書とCI設定も確認する",
    "status は CLI が担当する自動検査の結果だけを表す (pass / blocked / tool_error)",
    "publication_decision は常に人間レビュー要求とし、自動で公開承認しない",
    "検出結果に秘密値そのものを出力しない",
    "推奨質問の dismiss/snooze を採用先 .repo-preflight.json に記録し、次回から抑止する",
    "同梱 GitHub 設定ガイドの last_reviewed 期限切れを検知し、更新確認の質問を出す",
    "宣言設定がある repo では Markdown・README契約・影響マップ・SSOT生成物の整合性を検査する",
)

NON_GUARANTEES = (
    "秘密情報が存在しないことの完全保証 (独自形式・符号化・大容量blob・バイナリ内は見逃し得る)",
    "依存ライブラリの既知脆弱性",
    "第三者素材を公開する権利・ライセンス判断",
    "GitHubのbranch保護・review必須・Actions権限など remote 設定の現在状態",
    "GitHub製品変更・公式推奨のリアルタイム自動追従 (鮮度検知と更新確認までは行う)",
    "CIが remote で実際に成功したか",
    "障害通知先・復旧手順が実運用で機能すること",
    "README・個人情報・公開範囲の目視確認",
    "repo 固有の影響マップが宣言していない docs 更新要否の推測",
    "公開・push・merge・visibility変更・投稿の実行",
    "dismiss した推奨項目が将来も安全であること (期限切れ snooze や再発火があり得る)",
)

AUDIENCE_CHOICES = (
    ("public", "Web全体 (public化)"),
    ("team", "team / organization 共有"),
    ("client", "客先納品"),
    ("collaborator", "外部協力者 (期限付き)"),
    ("local", "ローカル確認のみ (見せる相手はまだ決めない)"),
)

MODE_CHOICES = (
    ("standard", "標準 preflight (文書・secret・path・identity・CI設定)"),
    ("release", "release準備 (標準 + README情報設計ゲート)"),
)

REQUIRED = ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "PREFLIGHT.md")
# PREFLIGHT.md は一般的な語のため、deployment preflight 手順書のような無関係な
# 同名fileが存在しうる。ファイル名の一致だけでreview記録とみなすと、検査記録が
# 無いrepositoryがpassする。テンプレートが埋め込むmarkerの実在まで確認する。
REVIEW_RECORD = "PREFLIGHT.md"
REVIEW_RECORD_MARKER = "<!-- repo-preflight:review-record -->"
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
    # git側の出力encodingをUTF-8に固定してから読む。i18n.logOutputEncodingが
    # 非UTF-8のrepositoryでも作者名を壊さない。不正byteはfail-closed比較に残す
    if args and args[0] == "git":
        args = (args[0], "-c", "i18n.logOutputEncoding=UTF-8", *args[1:])
    result = subprocess.run(
        args,
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        capture_output=True,
        shell=False,
    )
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


def repository_evidence_label(repo: Path) -> str:
    return sanitized_evidence_label(repo.name or "<repository>")


def effective_identity(value: str) -> str:
    return re.sub(r"\s+\d+\s+[+-]\d{4}$", "", value).strip()


def history_hits(
    repo: Path, rev_args: tuple[str, ...] = ("--all",)
) -> tuple[list[str], list[str]]:
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--no-object-names", *rev_args],
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
    # 空range (base == HEAD 等) は検査対象ゼロの成功。空入力をcat-fileへ
    # 渡すと出力行数が合わず inventory 失敗と誤判定するため、ここで返す
    if not inventory:
        return [], []
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
        ["git", "ls-files", "-z", "-v", "--stage"],
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
        tag, marker, entry = entry.partition(b" ")
        if not marker or len(tag) != 1:
            raise RuntimeError("git_worktree_inventory_failed")
        if tag.upper() == b"S":
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


def changed_working_tree_files(
    repo: Path, base_ref: str
) -> tuple[list[Path], set[Path]]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", f"{base_ref}...HEAD"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    paths: list[Path] = []
    deleted: set[Path] = set()
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        rel = raw_name.decode("utf-8", errors="surrogateescape")
        path = repo / rel
        if path.exists():
            mode_probe = subprocess.run(
                ["git", "ls-files", "--stage", "--", rel],
                cwd=repo,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                capture_output=True,
            )
            if mode_probe.returncode:
                raise RuntimeError("git_target_diff_inventory_failed")
            entries = [line for line in mode_probe.stdout.splitlines() if line]
            if len(entries) != 1:
                raise RuntimeError("git_target_diff_inventory_failed")
            mode = entries[0].split(maxsplit=1)[0]
            if mode in {"120000", "160000"}:
                continue
            if not mode.startswith("100"):
                raise RuntimeError("git_target_diff_inventory_failed")
            paths.append(path)
        else:
            deleted.add(path)
    return paths, deleted


def run_readme_release_gate(repo: Path) -> dict:
    script = Path(__file__).with_name("readme_release_gate.py")
    spec = importlib.util.spec_from_file_location("readme_release_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module.review(repo / "README.md")


def load_dialogue_gate():
    script = Path(__file__).with_name("dialogue_gate.py")
    spec = importlib.util.spec_from_file_location("dialogue_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    # importlib 経由でもモジュール属性参照できるよう登録する
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_preferences_module():
    script = Path(__file__).with_name("preferences.py")
    spec = importlib.util.spec_from_file_location("preferences", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_consistency_gate(repo: Path, base_ref: str | None) -> dict:
    script = Path(__file__).with_name("consistency_gate.py")
    spec = importlib.util.spec_from_file_location("consistency_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.check(repo, base_ref=base_ref)


def scan(
    repo: Path,
    expected_identity: str | None = None,
    *,
    release: bool = False,
    base_ref: str | None = None,
    consistency_base_ref: str | None = None,
) -> dict:
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
    }
    resolved_consistency_base_ref: str | None = None
    consistency_base_oid: str | None = None
    if consistency_base_ref and not base_ref:
        consistency_probe = run(
            repo, "git", "rev-parse", "--verify", f"{consistency_base_ref}^{{commit}}"
        )
        consistency_symbolic = run(
            repo, "git", "rev-parse", "--symbolic-full-name", consistency_base_ref
        )
        consistency_ancestor = run(
            repo, "git", "merge-base", "--is-ancestor", consistency_base_ref, "HEAD"
        )
        if (
            consistency_probe[0]
            or consistency_symbolic[0]
            or not consistency_symbolic[1].startswith("refs/remotes/origin/")
            or consistency_ancestor[0]
        ):
            return {"status": "tool_error", "issues": ["invalid_consistency_base_ref"]}
        resolved_consistency_base_ref = consistency_symbolic[1]
        consistency_base_oid = consistency_probe[1]
    if base_ref:
        base_probe = run(repo, "git", "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        symbolic_probe = run(repo, "git", "rev-parse", "--symbolic-full-name", base_ref)
        ancestor_probe = run(
            repo, "git", "merge-base", "--is-ancestor", base_ref, "HEAD"
        )
        if (
            base_probe[0]
            or symbolic_probe[0]
            or not symbolic_probe[1].startswith("refs/remotes/origin/")
            or ancestor_probe[0]
        ):
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": ["invalid_non_remote_or_non_ancestor_base_ref"],
            }
        identity_range = f"{base_ref}..HEAD"
        probes["identities"] = run(
            repo, "git", "log", "--format=%an <%ae>|%cn <%ce>", identity_range
        )
    else:
        probes["identities"] = run(
            repo, "git", "log", "--format=%an <%ae>|%cn <%ce>", "--all"
        )
    if any(code for code, _ in probes.values()):
        return {
            "status": "tool_error",
            "repo": repository_evidence_label(repo),
            "issues": ["git_probe_failed"],
        }
    head = probes["head"][1]
    dirty = probes["dirty"][1]
    identities = probes["identities"][1]
    resolved_base_ref: str | None = None
    base_oid: str | None = None
    if base_ref:
        resolved_base_ref = symbolic_probe[1]
        base_oid = base_probe[1]
    _, remote = run(repo, "git", "remote", "get-url", "origin")
    missing = (
        [] if base_ref else [name for name in REQUIRED if not (repo / name).is_file()]
    )
    invalid_documents: list[str] = []
    review_record = repo / REVIEW_RECORD
    if not base_ref and REVIEW_RECORD not in missing:
        try:
            record_text = review_record.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 読めない記録を「存在するからpass」にしない
            invalid_documents.append(REVIEW_RECORD)
        else:
            if REVIEW_RECORD_MARKER not in record_text:
                invalid_documents.append(REVIEW_RECORD)
    credential_finding_count = 0
    path_hits: list[str] = []
    try:
        if base_ref:
            paths, deleted_paths = changed_working_tree_files(repo, base_ref)
        else:
            paths = working_tree_files(repo)
            deleted_paths = deleted_working_tree_files(repo)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": repository_evidence_label(repo),
            "issues": ["git_worktree_inventory_failed"],
        }
    for path in paths:
        if path in deleted_paths:
            continue
        rel = path.relative_to(repo).as_posix()
        if not path.is_file():
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": [f"worktree_file_unreadable:{rel}"],
            }
        try:
            data = path.read_bytes()
        except OSError:
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": [f"worktree_file_unreadable:{rel}"],
            }
        if text_has(SECRET_PATTERNS, data):
            credential_finding_count += 1
        if text_has(PATH_PATTERNS, data):
            path_hits.append(sanitized_evidence_label(rel))
    try:
        history_credential_findings, history_path_hits = history_hits(
            repo, (f"{base_ref}..HEAD",) if base_ref else ("--all",)
        )
        credential_finding_count += len(history_credential_findings)
        path_hits.extend(history_path_hits)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": repository_evidence_label(repo),
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
    # 現在設定の名義はexpected_identity指定時だけ測る。identity未設定環境
    # (CI containerなど) をtool_errorにせず、判定はunknownでfail-closedに保つ
    effective_status = "not_evaluated"
    effective_mismatches: set[str] = set()
    if expected_identity:
        effective_probes = (
            run(repo, "git", "var", "GIT_AUTHOR_IDENT"),
            run(repo, "git", "var", "GIT_COMMITTER_IDENT"),
        )
        # probeは個別に評価する。片方が失敗しても、成功した側が示すmismatchは
        # 捨てない (失敗を理由にunknownへ丸めると既知の不一致が隠れる)
        probe_failed = False
        for code, value in effective_probes:
            if code:
                probe_failed = True
                continue
            identity = effective_identity(value)
            if identity != expected_identity:
                effective_mismatches.add(identity)
        if effective_mismatches:
            effective_status = "fail"
        elif probe_failed:
            effective_status = "unknown"
        else:
            effective_status = "pass"
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
            "status": (
                "not_evaluated"
                if base_ref
                else "pass" if not missing and not invalid_documents else "fail"
            ),
            "missing": missing,
            "invalid": invalid_documents,
        },
        "secret_scan": {
            "status": "pass" if credential_finding_count == 0 else "fail",
            "finding_count": credential_finding_count,
        },
        "personal_path_scan": {
            "status": "pass" if not path_hits else "fail",
            "files": path_hits,
        },
        "commit_identity": {
            "status": (
                "fail"
                if identity_mismatches or effective_status == "fail"
                else "unknown" if effective_status == "unknown" else "pass"
            ),
            "policy": "expected_identity" if expected_identity else "not_configured",
            "identity_count": len(identity_lines),
            "mismatch_count": len(identity_mismatches),
            "effective_identity": effective_status,
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
    checks["repository_consistency"] = run_consistency_gate(
        repo, consistency_base_ref or base_ref
    )
    if release:
        if (repo / "README.md").is_file():
            readme_report = run_readme_release_gate(repo)
            checks["readme_release_design"] = {
                "status": "pass" if readme_report["status"] == "pass" else "fail",
                "design_status": readme_report["status"],
                "release_gate": readme_report["release_gate"],
                "metrics": readme_report["metrics"],
                "findings": readme_report["findings"],
                "recommended_capabilities": readme_report["recommended_capabilities"],
                "human_visual_review_required": True,
            }
        else:
            checks["readme_release_design"] = {
                "status": "fail",
                "design_status": "blocked",
                "release_gate": "blocked_readme_missing",
                "findings": [
                    {
                        "code": "readme_missing",
                        "severity": "error",
                        "message": "README.mdがありません。",
                    }
                ],
                "recommended_capabilities": ["Template Creator"],
                "human_visual_review_required": True,
            }
    automated_check_names = {
        "clean_worktree",
        "secret_scan",
        "personal_path_scan",
        "commit_identity",
        "origin",
    }
    if not base_ref:
        automated_check_names.update(
            {"required_documents", "dependency_configuration", "ci_configuration"}
        )
    if release:
        automated_check_names.add("readme_release_design")
    automated_check_names.add("repository_consistency")
    tool_error = any(
        checks[name]["status"] == "tool_error" for name in automated_check_names
    )
    blocking = any(checks[name]["status"] == "fail" for name in automated_check_names)
    unknown = any(checks[name]["status"] == "unknown" for name in automated_check_names)
    return {
        "status": (
            "tool_error" if tool_error else "blocked" if blocking or unknown else "pass"
        ),
        "publication_decision": "blocked_human_review_required",
        "repo": repository_evidence_label(repo),
        "head": head,
        "scan_scope": (
            {
                "mode": "target_diff",
                "base_ref": sanitized_evidence_label(base_ref),
                "resolved_base_ref": sanitized_evidence_label(resolved_base_ref),
                "base_oid": base_oid,
            }
            if base_ref
            else {"mode": "repository"}
        ),
        "consistency_scope": (
            {
                "mode": "target_diff",
                "base_ref": sanitized_evidence_label(consistency_base_ref),
                "resolved_base_ref": sanitized_evidence_label(
                    resolved_consistency_base_ref
                ),
                "base_oid": consistency_base_oid,
            }
            if consistency_base_ref
            else {"mode": "same_as_scan"}
        ),
        "checks": checks,
    }


class ScanOptions:
    """検査実行時の選択。importlib経由のテスト読込でも使えるようdataclassを避ける。"""

    def __init__(
        self,
        repo: Path | None,
        release: bool = False,
        expected_identity: str | None = None,
        audience: str = "unspecified",
        interactive: bool = False,
        show_json: bool = True,
        intent: str | None = None,
        base_ref: str | None = None,
        consistency_base_ref: str | None = None,
    ) -> None:
        self.repo = repo
        self.release = release
        self.expected_identity = expected_identity
        self.audience = audience
        self.interactive = interactive
        self.show_json = show_json
        self.intent = intent
        self.base_ref = base_ref
        self.consistency_base_ref = consistency_base_ref


def boundary_sections() -> dict[str, list[str]]:
    return {
        "guarantees": list(GUARANTEES),
        "non_guarantees": list(NON_GUARANTEES),
    }


def format_boundary_text() -> str:
    lines = [
        "## 保証すること (CLIが担当する範囲)",
        *(f"- {item}" for item in GUARANTEES),
        "",
        "## 保証しないこと (別証拠・人間判断が要る)",
        *(f"- {item}" for item in NON_GUARANTEES),
    ]
    return "\n".join(lines)


def enrich_report(report: dict, options: ScanOptions) -> dict:
    """scan結果に V3 の境界メタデータと選択オプションを載せる。"""
    enriched = dict(report)
    enriched["schema"] = SCHEMA
    enriched["options"] = {
        "audience": options.audience,
        "mode": (
            "target_diff"
            if options.base_ref
            else "release" if options.release else "standard"
        ),
        "expected_identity_configured": bool(options.expected_identity),
        "interactive": options.interactive,
        "intent": options.intent,
        "base_ref": (
            sanitized_evidence_label(options.base_ref) if options.base_ref else None
        ),
        "consistency_base_ref": (
            sanitized_evidence_label(options.consistency_base_ref)
            if options.consistency_base_ref
            else None
        ),
    }
    enriched.update(boundary_sections())
    return enriched


def build_intent_dialogue(options: ScanOptions) -> dict:
    """AI が外部操作直前に使う質問パケットを作る。"""
    gate = load_dialogue_gate()
    prefs_mod = load_preferences_module()
    scan_report: dict | None = None
    needs_scan = gate.intent_needs_scan(options.intent) or options.repo is not None
    if needs_scan and options.repo is not None:
        release = options.release or gate.intent_uses_release_gate(options.intent)
        scan_kwargs = {
            "expected_identity": options.expected_identity,
            "release": release,
        }
        if options.base_ref:
            scan_kwargs["base_ref"] = options.base_ref
        if options.consistency_base_ref:
            scan_kwargs["consistency_base_ref"] = options.consistency_base_ref
        scan_report = scan(options.repo, **scan_kwargs)
        scan_report = enrich_report(
            scan_report,
            ScanOptions(
                repo=options.repo,
                release=release,
                expected_identity=options.expected_identity,
                audience=options.audience,
                interactive=options.interactive,
                show_json=options.show_json,
                intent=options.intent,
                base_ref=options.base_ref,
                consistency_base_ref=options.consistency_base_ref,
            ),
        )
    elif needs_scan and options.repo is None:
        scan_report = None
    preferences = prefs_mod.load_preferences(options.repo)
    github_baseline = prefs_mod.github_baseline_status(
        prefs_mod.default_github_baseline_path()
    )
    boundaries = boundary_sections()
    return gate.build_dialogue(
        intent=options.intent,
        scan=scan_report,
        audience=options.audience,
        guarantees=boundaries["guarantees"],
        non_guarantees=boundaries["non_guarantees"],
        preferences=preferences,
        github_baseline=github_baseline,
        preferences_module=prefs_mod,
    )


def dialogue_exit_code(dialogue: dict) -> int:
    status = dialogue.get("status")
    if status == "ready_after_confirmation":
        return 0
    if status == "blocked":
        return 1
    if status == "needs_human_input":
        return 1
    return 2


def format_check_line(name: str, check: dict) -> str:
    status = check.get("status", "unknown")
    detail_parts: list[str] = []
    if name == "secret_scan" and "finding_count" in check:
        detail_parts.append(f"findings={check['finding_count']}")
    if name == "required_documents":
        if check.get("missing"):
            detail_parts.append(f"missing={','.join(check['missing'])}")
        if check.get("invalid"):
            detail_parts.append(f"invalid={','.join(check['invalid'])}")
    if name == "personal_path_scan" and check.get("files"):
        detail_parts.append(f"files={len(check['files'])}")
    if name == "commit_identity":
        detail_parts.append(f"identities={check.get('identity_count', 0)}")
        if check.get("mismatch_count"):
            detail_parts.append(f"mismatches={check['mismatch_count']}")
    if name == "ci_configuration":
        detail_parts.append(f"workflows={check.get('workflow_count', 0)}")
    if check.get("reason"):
        detail_parts.append(str(check["reason"]))
    detail = f" ({'; '.join(detail_parts)})" if detail_parts else ""
    return f"- {name}: {status}{detail}"


def format_human_report(report: dict) -> str:
    lines = [
        "# Repo Preflight 結果",
        "",
        f"schema: {report.get('schema', SCHEMA)}",
        f"status: {report.get('status')}",
        f"publication_decision: {report.get('publication_decision', 'n/a')}",
    ]
    if report.get("repo"):
        lines.append(f"repo: {report['repo']}")
    if report.get("head"):
        lines.append(f"head: {report['head']}")
    options = report.get("options") or {}
    if options:
        lines.append(
            "options: "
            f"audience={options.get('audience')}, "
            f"mode={options.get('mode')}, "
            f"expected_identity_configured={options.get('expected_identity_configured')}"
        )
    lines.extend(["", format_boundary_text(), ""])
    if report.get("issues"):
        lines.append("## issues")
        lines.extend(f"- {issue}" for issue in report["issues"])
        lines.append("")
    checks = report.get("checks") or {}
    if checks:
        lines.append("## checks")
        for name, check in checks.items():
            if isinstance(check, dict):
                lines.append(format_check_line(name, check))
        lines.append("")
    lines.extend(
        [
            "## 読み方",
            "- status:pass は「このCLIが担当するローカル自動検査に合格した」だけを意味します。",
            "- publication_decision は常に人間レビュー要求です。pass だけを根拠に公開しないでください。",
            "- unknown / fail の項目は、別の証拠または人が埋めるまで先へ進めません。",
        ]
    )
    return "\n".join(lines)


def _emit(stream: TextIO, text: str) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def prompt_from_stdin(prompt: str = "") -> str:
    """対話入力。promptはstderrへ出し、stdoutのJSONを汚さない。"""
    if prompt:
        sys.stderr.write(prompt)
        sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        raise EOFError("EOF while reading interactive input")
    return line.rstrip("\r\n")


def prompt_text(
    message: str,
    *,
    default: str | None = None,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input_fn(f"{message}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        output_fn("値を入力してください。")


def prompt_choice(
    title: str,
    choices: tuple[tuple[str, str], ...],
    *,
    default_key: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    keys = [key for key, _ in choices]
    if default_key not in keys:
        raise ValueError(f"unknown default choice: {default_key}")
    output_fn(title)
    for index, (key, label) in enumerate(choices, start=1):
        marker = " (default)" if key == default_key else ""
        output_fn(f"  {index}) {key} — {label}{marker}")
    while True:
        raw = input_fn(f"番号または key [{default_key}]: ").strip().lower()
        if not raw:
            return default_key
        if raw.isdigit():
            position = int(raw)
            if 1 <= position <= len(choices):
                return choices[position - 1][0]
        for key, _ in choices:
            if raw == key.lower():
                return key
        output_fn("選択肢の番号または key を入力してください。")


def prompt_yes_no(
    message: str,
    *,
    default: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        raw = input_fn(f"{message} [{default_label}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "はい"}:
            return True
        if raw in {"n", "no", "いいえ"}:
            return False
        output_fn("y または n で答えてください。")


def collect_interactive_options(
    *,
    initial_repo: Path | None = None,
    initial_release: bool = False,
    initial_identity: str | None = None,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    """対話で ScanOptions を集める。入出力はテスト差し替え可能。"""
    if output_fn is None:
        output_fn = lambda text: _emit(sys.stderr, text)

    output_fn("Repo Preflight v3 — 対話モード")
    output_fn("見せる相手を広げる前のローカル検査です。公開や push は実行しません。")
    output_fn("")
    output_fn(format_boundary_text())
    output_fn("")

    audience = prompt_choice(
        "見せる相手 (audience) を選んでください。必要な人間確認の観点が変わります。",
        AUDIENCE_CHOICES,
        default_key="local",
        input_fn=input_fn,
        output_fn=output_fn,
    )
    mode = prompt_choice(
        "検査モードを選んでください。",
        MODE_CHOICES,
        default_key="release" if initial_release else "standard",
        input_fn=input_fn,
        output_fn=output_fn,
    )
    release = mode == "release"

    default_repo = str(initial_repo) if initial_repo is not None else str(Path.cwd())
    repo_text = prompt_text(
        "検査対象リポジトリのパス",
        default=default_repo,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    repo = Path(repo_text)

    use_identity = prompt_yes_no(
        "全commitの作者/committer名義を固定値と照合しますか?",
        default=bool(initial_identity),
        input_fn=input_fn,
        output_fn=output_fn,
    )
    expected_identity = initial_identity
    if use_identity:
        expected_identity = prompt_text(
            "期待する名義 (例: Example <dev@example.invalid>)",
            default=initial_identity or "",
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if not expected_identity:
            expected_identity = None
    else:
        expected_identity = None

    show_json = prompt_yes_no(
        "結果のJSONも表示しますか?",
        default=True,
        input_fn=input_fn,
        output_fn=output_fn,
    )

    output_fn("")
    output_fn("選択内容:")
    output_fn(f"- audience: {audience}")
    output_fn(f"- mode: {mode}")
    output_fn(f"- repo: {repo}")
    output_fn(
        f"- expected_identity: {expected_identity if expected_identity else '(未設定)'}"
    )
    output_fn(f"- show_json: {show_json}")
    if not prompt_yes_no(
        "この内容で検査を実行しますか?",
        default=True,
        input_fn=input_fn,
        output_fn=output_fn,
    ):
        raise SystemExit(1)

    return ScanOptions(
        repo=repo,
        release=release,
        expected_identity=expected_identity,
        audience=audience,
        interactive=True,
        show_json=show_json,
    )


def build_parser() -> argparse.ArgumentParser:
    gate = load_dialogue_gate()
    parser = argparse.ArgumentParser(
        description=(
            "Gitリポジトリを見せる相手を広げる前の読み取り専用 preflight。"
            "AI実装フローでは --intent で操作直前の質問パケットを出す。"
            "保証範囲と非保証範囲を常に表示する。"
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        help="検査対象リポジトリ。--intent create_repo 以外では必須",
    )
    parser.add_argument(
        "--intent",
        choices=list(gate.INTENTS),
        help=(
            "AIが実行しようとする操作。"
            "create_repo / push / open_pr / merge / publish / release。"
            "指定時は不足設定と推奨案を質問パケットとして返す"
        ),
    )
    parser.add_argument(
        "--base-ref",
        help=(
            "既存repoの今回差分だけを検査するbase ref。"
            "--intent push/open_pr/merge専用で、baseはHEADの祖先でなければならない"
        ),
    )
    parser.add_argument(
        "--consistency-base-ref",
        help=(
            "repo全体scanを狭めず、整合性のchange-sensitive検査だけに使うremote base ref。"
            "publish/release向け"
        ),
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="コンソール補助: 検査オプションをTTYで選ぶ (本体は --intent のエージェント対話)",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="release準備としてREADME情報設計ゲートも自動実行する",
    )
    parser.add_argument(
        "--expected-identity",
        help='Expected Git author and committer identity, for example "Example <dev@example.com>"',
    )
    parser.add_argument(
        "--audience",
        choices=[key for key, _ in AUDIENCE_CHOICES],
        default="unspecified",
        help="見せる相手。publish intent やメタデータに使う",
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="人間/エージェント向け要約を stderr に出し、JSONは stdout に出す",
    )
    parser.add_argument(
        "--record-dismissal",
        metavar="PROPOSAL_ID",
        help=(
            "採用先 repo の .repo-preflight.json に dismiss を記録して終了。"
            "--repo と --dismissal-mode が必要"
        ),
    )
    parser.add_argument(
        "--dismissal-mode",
        choices=["forever", "7d", "30d", "90d"],
        help="--record-dismissal と併用。forever / 7d / 30d / 90d",
    )
    parser.add_argument(
        "--dismissal-reason",
        default="",
        help="dismiss 記録の理由 (任意)",
    )
    return parser


def resolve_options(
    args: argparse.Namespace,
    *,
    stdin_is_tty: bool,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    intent = getattr(args, "intent", None)
    base_ref = getattr(args, "base_ref", None)
    consistency_base_ref = getattr(args, "consistency_base_ref", None)
    if base_ref and consistency_base_ref:
        raise SystemExit("error: --base-ref and --consistency-base-ref are exclusive")
    if base_ref and intent not in {"push", "open_pr", "merge"}:
        raise SystemExit("error: --base-ref requires --intent push, open_pr, or merge")
    if consistency_base_ref and not (
        intent in {"publish", "release"} or (intent is None and bool(args.release))
    ):
        raise SystemExit(
            "error: --consistency-base-ref requires publish/release intent or --release"
        )
    # intent モードはエージェント対話が本体。TTYメニューは使わない
    want_console = bool(
        args.interactive or (args.repo is None and stdin_is_tty and not intent)
    )
    if want_console:
        options = collect_interactive_options(
            initial_repo=args.repo,
            initial_release=bool(args.release),
            initial_identity=args.expected_identity,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        options.intent = intent
        options.base_ref = base_ref
        options.consistency_base_ref = consistency_base_ref
        return options

    gate = load_dialogue_gate()
    if intent and not gate.intent_needs_scan(intent) and args.repo is None:
        return ScanOptions(
            repo=None,
            release=bool(args.release),
            expected_identity=args.expected_identity,
            audience=args.audience,
            interactive=False,
            show_json=True,
            intent=intent,
            base_ref=base_ref,
            consistency_base_ref=consistency_base_ref,
        )
    if args.repo is None:
        raise SystemExit(
            "error: --repo is required "
            "(create_repo intent のみ省略可。コンソール補助は --interactive)"
        )
    return ScanOptions(
        repo=args.repo,
        release=bool(args.release),
        expected_identity=args.expected_identity,
        audience=args.audience,
        interactive=False,
        show_json=True,
        intent=intent,
        base_ref=base_ref,
        consistency_base_ref=consistency_base_ref,
    )


def report_exit_code(report: dict) -> int:
    if report["status"] == "pass":
        return 0
    if report["status"] == "tool_error":
        return 2
    return 1


def main(
    argv: list[str] | None = None,
    *,
    stdin_is_tty: bool | None = None,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    console: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    out = stdout if stdout is not None else sys.stdout
    err = console if console is not None else sys.stderr
    tty = sys.stdin.isatty() if stdin_is_tty is None else stdin_is_tty

    parser = build_parser()
    args = parser.parse_args(argv)

    # dismiss 記録専用パス (対話の回答を機械反映)
    if args.record_dismissal:
        if args.repo is None or not args.dismissal_mode:
            _emit(
                err,
                "error: --record-dismissal requires --repo and --dismissal-mode",
            )
            return 2
        prefs_mod = load_preferences_module()
        try:
            prefs = prefs_mod.load_preferences(args.repo)
            prefs = prefs_mod.record_dismissal(
                prefs,
                args.record_dismissal,
                mode=args.dismissal_mode,
                reason=args.dismissal_reason or "",
            )
            path = prefs_mod.save_preferences(args.repo, prefs)
        except Exception as exc:
            _emit(
                err,
                f"error: failed to record dismissal ({type(exc).__name__})",
            )
            return 2
        _emit(
            out,
            json.dumps(
                {
                    "schema": prefs_mod.PREFERENCES_SCHEMA,
                    "status": "recorded",
                    "path": path.name,
                    "proposal_id": args.record_dismissal,
                    "dismissal": prefs["dismissals"][args.record_dismissal],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return 0

    try:
        options = resolve_options(
            args,
            stdin_is_tty=tty,
            input_fn=input_fn,
            output_fn=lambda text: _emit(err, text),
        )
    except EOFError:
        _emit(err, "error: interactive input ended before options were complete")
        return 2
    except SystemExit as exc:
        # argparse / 対話キャンセル。message付きは人間向けに stderr へ
        if exc.code not in (0, None) and not isinstance(exc.code, int):
            _emit(err, str(exc.code))
            return 2
        if isinstance(exc.code, int):
            return exc.code
        return 0

    # AI 操作直前ゲート: 不足設定を質問パケットとして返す
    if options.intent:
        try:
            dialogue = build_intent_dialogue(options)
        except Exception as exc:
            dialogue = {
                "schema": "repo-preflight.dialogue/v3",
                "intent": options.intent,
                "status": "blocked",
                "publication_decision": "blocked_human_review_required",
                "issues": [f"unexpected_exception:{type(exc).__name__}"],
                "proposals": [],
                "confirmations": [],
            }
            _emit(out, json.dumps(dialogue, ensure_ascii=False, indent=2))
            return 2
        gate = load_dialogue_gate()
        if args.human or options.interactive:
            _emit(err, gate.format_dialogue_for_agent(dialogue))
        _emit(out, json.dumps(dialogue, ensure_ascii=False, indent=2))
        return dialogue_exit_code(dialogue)

    try:
        scan_kwargs = {
            "expected_identity": options.expected_identity,
            "release": options.release,
        }
        if options.base_ref:
            scan_kwargs["base_ref"] = options.base_ref
        if options.consistency_base_ref:
            scan_kwargs["consistency_base_ref"] = options.consistency_base_ref
        report = scan(options.repo, **scan_kwargs)
    except Exception as exc:  # 予期しない例外もexit 2の検査失敗として扱う
        # 例外messageはpath/secretを含み得るため型名だけ返す
        report = {
            "status": "tool_error",
            "issues": [f"unexpected_exception:{type(exc).__name__}"],
        }
    report = enrich_report(report, options)

    human_wanted = options.interactive or bool(args.human)
    if human_wanted:
        _emit(err, format_human_report(report))
        if options.interactive and not options.show_json:
            return report_exit_code(report)
        if options.interactive:
            _emit(err, "")
            _emit(err, "## JSON")

    # 機械可読の正本は常に stdout の JSON (対話で JSON 非表示を選んだ場合のみ省略)
    if not options.interactive or options.show_json:
        _emit(out, json.dumps(report, ensure_ascii=False, indent=2))
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())

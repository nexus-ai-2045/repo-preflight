from __future__ import annotations

import json
import os
import re
import traceback
from typing import Callable, Mapping, NamedTuple

SCHEMA = "repo-preflight.scan/v3"

GUARANTEES = (
    "ローカルGitの現在treeと履歴を読み取り専用で検査する",
    "選択した検査scopeでsecret候補・個人path・作者名義を機械判定し、repo全体modeでは必須文書とCI設定も確認する",
    "repo全体modeでは作業treeの Live2D Cubism .cmo3 を展開して個人pathを判定し、展開できないfileは unknown にする",
    "status は CLI が担当する自動検査の結果だけを表す (pass / blocked / tool_error)",
    "publication_decision は常に人間レビュー要求とし、自動で公開承認しない",
    "検出結果に秘密値そのものを出力しない",
    "推奨質問の dismiss/snooze を採用先 .repo-preflight.json に記録し、次回から抑止する",
    "同梱 GitHub 設定ガイドの last_reviewed 期限切れを検知し、更新確認の質問を出す",
    "configure_settings intent ではGitHub APIをread-only取得し、profile差分と個別previewを返す",
    "宣言設定がある repo では Markdown・README契約・影響マップ・SSOT生成物の整合性を検査する",
)

NON_GUARANTEES = (
    "秘密情報が存在しないことの完全保証 (独自形式・符号化・大容量blob・バイナリ内、英数字・_・- に直結した sk- 形式の鍵は見逃し得る)",
    "個人pathが存在しないことの完全保証 (.cmo3 の展開はrepo全体modeの作業treeだけ。履歴・--base-ref差分内の .cmo3、他の独自形式・圧縮・バイナリ内は見逃し得る)",
    "依存ライブラリの既知脆弱性",
    "第三者素材を公開する権利・ライセンス判断",
    "通常scanにおけるGitHub remote設定全体の現在状態 (configure_settings intentで別途取得。個人path例外のv2はprivate・v3はpublic合成fixture限定をlive確認)",
    "GitHub製品変更・公式推奨のリアルタイム自動追従 (鮮度検知と更新確認までは行う)",
    "CIが remote で実際に成功したか",
    "障害通知先・復旧手順が実運用で機能すること",
    "README・個人情報・公開範囲の目視確認",
    "repo 固有の影響マップが宣言していない docs 更新要否の推測",
    "公開・push・merge・visibility変更・投稿の実行",
    "dismiss した推奨項目が将来も安全であること (期限切れ snooze や再発火があり得る)",
)

ALWAYS_GATED_CHECKS = (
    "clean_worktree",
    "secret_scan",
    "personal_path_scan",
    "commit_identity",
    "identity_gate",
    "origin",
)
REPOSITORY_SCOPE_CHECKS = (
    "required_documents",
    "dependency_configuration",
    "ci_configuration",
)
RELEASE_CHECKS = ("readme_release_design", "readme_release_freshness")
IDENTITY_GATE_COVERED_BY_PUBLIC_ATTRIBUTION = {
    "status": "not_configured",
    "reason": "private_patterns_not_configured",
    "covered_by": "public_attribution",
}

_MODULE_NAME_SAFE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}\Z")
_FRAME_BASENAME_UNSAFE = re.compile(r"[:/\\\r\n\x00]")


class Gating(NamedTuple):
    names: frozenset
    identity_gate_override: dict | None


def gating(
    *,
    identity_gate: Mapping,
    allowed_identities_configured: bool,
    require_private_identity: bool,
    private_identity_report: Mapping,
    target_diff: bool,
    release: bool,
) -> Gating:
    names = set(ALWAYS_GATED_CHECKS)
    override = None
    if (
        identity_gate.get("reason") == "private_patterns_not_configured"
        and allowed_identities_configured
        and not require_private_identity
    ):
        override = dict(IDENTITY_GATE_COVERED_BY_PUBLIC_ATTRIBUTION)
        names.discard("identity_gate")
    if require_private_identity or private_identity_report["status"] != (
        "not_configured"
    ):
        names.add("private_identity_scan")
    if allowed_identities_configured:
        names.add("public_attribution")
    if not target_diff:
        names.update(REPOSITORY_SCOPE_CHECKS)
    if release:
        names.update(RELEASE_CHECKS)
    names.add("repository_consistency")
    return Gating(frozenset(names), override)


def aggregate_status(checks: Mapping[str, Mapping], names) -> str:
    tool_error = any(checks[name]["status"] == "tool_error" for name in names)
    blocking = any(checks[name]["status"] == "fail" for name in names)
    unknown = any(checks[name]["status"] == "unknown" for name in names)
    return "tool_error" if tool_error else "blocked" if blocking or unknown else "pass"


def sanitized_evidence_label(
    label: str,
    *,
    sensitive: Callable[[bytes], bool],
    private_name: bool = False,
) -> str:
    data = label.encode("utf-8", errors="surrogatepass")
    if private_name or sensitive(data):
        return "<redacted-path>"
    return label


def sanitized_ref_report(
    report: dict,
    refs: tuple,
    private_identity,
    *,
    sensitive: Callable[[bytes], bool],
) -> dict:
    labels = tuple(dict.fromkeys(ref for ref in refs if ref))
    if not labels:
        return report
    replacements = []
    unknown = 0
    for label in labels:
        status = (
            private_identity.label_status(label)
            if private_identity is not None
            else "unknown"
        )
        unknown += status == "unknown"
        safe = sanitized_evidence_label(
            label, sensitive=sensitive, private_name=status != "public"
        )
        if safe != label:
            pattern = re.compile(
                r"(?<![\w./-])" + re.escape(label) + r"(?=\.\.|$|[^\w./-])"
            )
            replacements.append((len(label), pattern, safe))
    replacements.sort(key=lambda item: item[0], reverse=True)

    ref_label_fields = {
        "base_ref",
        "resolved_base_ref",
        "consistency_base_ref",
        "identity_base_ref",
        "git_object_scope",
    }

    def scrub(value, field=None):
        if isinstance(value, str) and field in ref_label_fields:
            for _, pattern, safe in replacements:
                value = pattern.sub(lambda match: safe, value)
            return value
        if isinstance(value, dict):
            return {key: scrub(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item, field) for item in value]
        return value

    sanitized = scrub(report)
    sanitized["ref_label_privacy"] = {
        "status": "unknown" if unknown else "complete",
        "checked_ref_count": len(labels),
        "unverified_ref_count": unknown,
    }
    return sanitized


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


def enrich_report(
    report: dict,
    options,
    *,
    label_checker,
    sensitive: Callable[[bytes], bool],
) -> dict:
    def label(value):
        return sanitized_evidence_label(value, sensitive=sensitive)

    enriched = dict(report)
    enriched["schema"] = SCHEMA
    enriched["options"] = {
        "audience": options.audience,
        "mode": (
            "private_committed_submission"
            if options.private_submission
            else (
                "target_diff"
                if options.base_ref
                else "release" if options.release else "standard"
            )
        ),
        "expected_identity_configured": bool(options.expected_identity),
        "interactive": options.interactive,
        "intent": options.intent,
        "private_submission": options.private_submission,
        "allow_expected_private_commit_identity": options.allow_expected_private_commit_identity,
        "media_inspection": options.media_inspection,
        "base_ref": (label(options.base_ref) if options.base_ref else None),
        "consistency_base_ref": (
            label(options.consistency_base_ref)
            if options.consistency_base_ref
            else None
        ),
        "identity_base_ref": (
            label(options.identity_base_ref) if options.identity_base_ref else None
        ),
        "github_settings_profile": options.github_settings_profile,
        "reviewed_secret_exceptions_configured": options.reviewed_secret_exceptions
        is not None,
    }
    enriched.update(boundary_sections())
    if options.media_inspection == "metadata":
        enriched["guarantees"].append(
            "明示private媒体は形式・metadata・境界sample・全SHAを確認する"
        )
        enriched["non_guarantees"].append(
            "metadata方針は圧縮payload全体、映像の文字・音声の意味の個人情報判定を行わない"
        )
    return sanitized_ref_report(
        enriched,
        (options.base_ref, options.consistency_base_ref, options.identity_base_ref),
        label_checker,
        sensitive=sensitive,
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
    if name == "personal_path_scan" and check.get("unscanned_files"):
        detail_parts.append(f"unscanned={len(check['unscanned_files'])}")
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
                if check.get("status") not in {"fail", "unknown", "tool_error"}:
                    continue
                findings = check.get("findings") or []
                if not isinstance(findings, list):
                    findings = []
                for finding in findings[:50]:
                    if not isinstance(finding, dict):
                        continue
                    location = []
                    for field in ("source", "commit", "path", "line", "field", "rule"):
                        value = finding.get(field)
                        if value is not None:
                            location.append(
                                f"{field}={json.dumps(value, ensure_ascii=False)}"
                            )
                    if finding.get("line") is None:
                        location.append("line=未確定")
                    kind = json.dumps(finding.get("kind", "検出"), ensure_ascii=False)
                    lines.append(f"  - {kind}: {'; '.join(location)}")
                for location in (check.get("locations") or check.get("files") or [])[
                    :50
                ]:
                    if isinstance(location, str):
                        lines.append(
                            f"  - location={json.dumps(location, ensure_ascii=False)}; line=未確定"
                        )
                omitted = check.get("findings_truncated", 0) + check.get(
                    "locations_truncated", 0
                )
                if omitted:
                    lines.append(
                        "  - diagnostics truncated=true（以降の位置は省略・未確定）"
                    )
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


def unexpected_exception_issue(exc: BaseException) -> str:
    issue = f"unexpected_exception:{type(exc).__name__}"
    if not isinstance(exc, ModuleNotFoundError):
        return issue
    name = getattr(exc, "name", None)
    if isinstance(name, str) and _MODULE_NAME_SAFE.fullmatch(name):
        issue += f":name={name}"
    tb = exc.__traceback__
    if tb is None:
        return issue
    frames = traceback.extract_tb(tb)
    if not frames:
        return issue
    last = frames[-1]
    base = os.path.basename(last.filename)
    lineno = last.lineno
    if (
        isinstance(base, str)
        and base
        and base not in {".", ".."}
        and len(base) <= 180
        and _FRAME_BASENAME_UNSAFE.search(base) is None
        and isinstance(lineno, int)
        and lineno > 0
    ):
        issue += f":at={base}:{lineno}"
    return issue


def report_exit_code(report: dict) -> int:
    if report["status"] == "pass":
        return 0
    if report["status"] == "tool_error":
        return 2
    return 1

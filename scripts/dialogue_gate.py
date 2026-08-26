"""AI 実装フロー向けの対話ゲート。

TTY メニューではなく、エージェントが repo 作成 / push / PR / merge / 公開 に
進む直前に自動発火し、未設定項目と推奨設定を「質問パケット」として返す。
エージェントは required の質問に人間の回答を得るまで外部操作を実行しない。
"""

from __future__ import annotations

import re
from typing import Any  # preferences_module は duck typing

DIALOGUE_SCHEMA = "repo-preflight.dialogue/v3"

# エージェントが「この操作に進む前」に必ず発火する intent
INTENTS = (
    "create_repo",
    "push",
    "open_pr",
    "merge",
    "configure_settings",
    "publish",
    "release",
)

INTENT_LABELS = {
    "create_repo": "GitHub リポジトリ作成",
    "push": "remote への push",
    "open_pr": "Pull Request 作成",
    "merge": "PR merge",
    "configure_settings": "GitHub repository Settings の変更準備",
    "publish": "見せる相手を広げる (public化 / 共有 / 納品)",
    "release": "release / tag / 告知準備",
}

AGENT_INSTRUCTIONS = (
    "このパケットを受け取ったら、まず guarantees / non_guarantees をユーザーに短く示す。",
    "proposals と confirmations を番号付きで提示し、1問ずつまたはまとめて回答を取る。",
    "status が needs_human_input または blocked の間は、intent の外部操作を実行しない。",
    "回答が yes / approve でも、push・PR・merge・visibility変更・投稿は別ゲートとして再確認する。",
    "secret / personal_path / 危険な履歴操作は『無視して進む』選択肢を出さない。",
    "テンプレート作成や設定変更は、ユーザーが明示的に yes した項目だけ行う。",
    "configure_settings は inspect / compare / preview まで。各設定の変更は別承認後にだけ実行する。",
    "dismiss_30d / dismiss_90d / dismiss_forever を選ばれたら、採用先の .repo-preflight.json に記録する"
    "（--record-dismissal または preferences API）。secret 等 dismissible=false は記録しない。",
    "suppressed_proposals は『次から出さない』済み。必要なら設定ファイルを見せて解除方法を案内する。",
    "GitHub 設定ガイドが stale なら references/github-settings.md を公式docsと突き合わせて更新し、"
    "last_reviewed を進める。自動で GitHub 全変更を追従したことにはしない。",
    "pass や ready_after_confirmation を公開承認と解釈しない。",
)


def _proposal(
    *,
    id: str,
    kind: str,
    severity: str,
    question: str,
    current: Any,
    proposed: Any,
    options: list[dict[str, str]],
    default: str,
    blocks_intent: bool = True,
    auto_apply_safe: bool = False,
    why: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": id,
        "kind": kind,
        "severity": severity,
        "question": question,
        "current": current,
        "proposed": proposed,
        "options": options,
        "default": default,
        "blocks_intent": blocks_intent,
        "auto_apply_safe": auto_apply_safe,
    }
    if why:
        item["why"] = why
    return item


def _yes_no_options(yes_label: str, no_label: str) -> list[dict[str, str]]:
    return [
        {"id": "yes", "label": yes_label},
        {"id": "no", "label": no_label},
    ]


def _intent_release_mode(intent: str) -> bool:
    return intent in {"publish", "release"}


def build_proposals_from_scan(
    *,
    intent: str,
    scan: dict[str, Any],
    audience: str = "unspecified",
) -> list[dict[str, Any]]:
    """scan 結果と intent から、人間への提案質問を組み立てる。"""
    proposals: list[dict[str, Any]] = []
    checks = scan.get("checks") or {}
    status = scan.get("status")

    if status == "tool_error":
        issues = scan.get("issues") or []
        if "repo_path_is_not_repository_root" in issues:
            # 読めないのではなく、別 repository の中を指している。汎用の
            # 「読める状態に直す」では何を直せばよいか伝わらない
            root = scan.get("repository_root") or "<repository>"
            proposals.append(
                _proposal(
                    id="fix_repo_path_not_repository_root",
                    kind="tool_error",
                    severity="required",
                    question=(
                        f"指定した path は repository root ではなく、repository "
                        f"`{root}` の中の一部です。`{root}` 全体を検査対象にするか、"
                        "その path を独立した repository にしてから"
                        f"「{INTENT_LABELS.get(intent, intent)}」へ進みますか?"
                    ),
                    current={
                        "issues": issues,
                        "repository_root": root,
                    },
                    proposed="retarget_repository_root_then_rerun_preflight",
                    options=[
                        {
                            "id": "retarget_root",
                            "label": f"`{root}` 全体を対象にして再検査する",
                        },
                        {
                            "id": "init_repository",
                            "label": "指定した path を独立した repository にする",
                        },
                        {"id": "no", "label": "中止する"},
                    ],
                    default="no",
                    why=(
                        "囲っている repository の判定を、指した path の判定として"
                        "返さない"
                    ),
                )
            )
            return proposals
        proposals.append(
            _proposal(
                id="fix_tool_error",
                kind="tool_error",
                severity="required",
                question=(
                    "検査自体が失敗しています。Git リポジトリとして読める状態に直してから"
                    f"「{INTENT_LABELS.get(intent, intent)}」へ進みますか?"
                ),
                current={"issues": scan.get("issues", [])},
                proposed="fix_repository_access_then_rerun_preflight",
                options=_yes_no_options(
                    "直して再検査する",
                    "中止する",
                ),
                default="yes",
                why="tool_error のまま外部操作へ進まない",
            )
        )
        return proposals

    docs = checks.get("required_documents") or {}
    for name in docs.get("missing") or []:
        proposals.append(
            _proposal(
                id=f"create_missing_{name.replace('.', '_').lower()}",
                kind="missing_artifact",
                severity="required",
                question=(
                    f"{name} がありません。"
                    f"{INTENT_LABELS.get(intent, intent)} の前にテンプレートから作成しますか?"
                ),
                current=None,
                proposed={
                    "action": "create_from_template_or_scaffold",
                    "path": name,
                    "template_hint": (
                        "assets/PREFLIGHT.template.md"
                        if name == "PREFLIGHT.md"
                        else f"create minimal {name}"
                    ),
                },
                options=_yes_no_options(
                    f"{name} を作成する",
                    "作成せず停止する",
                ),
                default="yes",
                why="必須文書欠落のまま見せる相手を広げない",
            )
        )
    for name in docs.get("invalid") or []:
        proposals.append(
            _proposal(
                id=f"fix_invalid_{name.replace('.', '_').lower()}",
                kind="invalid_artifact",
                severity="required",
                question=(
                    f"{name} はありますが review 記録として無効です"
                    "（PREFLIGHT なら marker 不足など）。"
                    "正しい内容に直しますか?"
                ),
                current={"path": name, "status": "invalid"},
                proposed={"action": "repair_review_record", "path": name},
                options=_yes_no_options("直す", "直さず停止する"),
                default="yes",
            )
        )

    clean = checks.get("clean_worktree") or {}
    if clean.get("status") == "fail" and intent in {
        "push",
        "open_pr",
        "merge",
        "publish",
        "release",
    }:
        proposals.append(
            _proposal(
                id="handle_dirty_worktree",
                kind="worktree_state",
                severity="required",
                question=(
                    "未コミットの変更があります。"
                    "コミットしてから進めますか? (捨てる操作は提案しません)"
                ),
                current="dirty",
                proposed={"action": "commit_relevant_changes_then_rerun"},
                options=[
                    {"id": "commit", "label": "関連変更をコミットして再検査"},
                    {"id": "stop", "label": "今は中止する"},
                ],
                default="commit",
            )
        )

    secret = checks.get("secret_scan") or {}
    if secret.get("status") == "fail":
        proposals.append(
            _proposal(
                id="resolve_secret_findings",
                kind="security_hold",
                severity="required",
                question=(
                    f"secret 候補が {secret.get('finding_count', 0)} 件あります。"
                    "履歴を含めて除去・無効化してから再検査しますか?"
                    "（無視して進む選択肢はありません）"
                ),
                current={"finding_count": secret.get("finding_count", 0)},
                proposed={"action": "remove_or_rotate_secrets_then_rerun"},
                options=[
                    {"id": "fix", "label": "除去・rotate して再検査"},
                    {"id": "stop", "label": "中止する"},
                ],
                default="fix",
                why="secret 検出時は fail-closed。ignore を出さない",
            )
        )

    paths = checks.get("personal_path_scan") or {}
    if paths.get("status") == "fail":
        proposals.append(
            _proposal(
                id="resolve_personal_paths",
                kind="privacy_hold",
                severity="required",
                question=(
                    "個人環境の絶対パス候補が見つかりました。"
                    "公開・共有前に除去しますか?（無視して進む選択肢はありません）"
                ),
                current={"file_count": len(paths.get("files") or [])},
                proposed={"action": "redact_personal_paths_then_rerun"},
                options=[
                    {"id": "fix", "label": "除去して再検査"},
                    {"id": "stop", "label": "中止する"},
                ],
                default="fix",
            )
        )

    identity = checks.get("commit_identity") or {}
    if identity.get("status") == "fail":
        proposals.append(
            _proposal(
                id="resolve_identity_mismatch",
                kind="identity_hold",
                severity="required",
                question=(
                    "commit 作者/committer 名義が期待値と一致しません。"
                    "公開名義ポリシーを確認し、未公開範囲だけ修正するか中止しますか?"
                    "（履歴の force rewrite は別承認なしで提案しません）"
                ),
                current={
                    "mismatch_count": identity.get("mismatch_count"),
                    "effective_identity": identity.get("effective_identity"),
                },
                proposed={"action": "align_identity_without_silent_history_rewrite"},
                options=[
                    {
                        "id": "review_policy",
                        "label": "名義ポリシーを確認してから決める",
                    },
                    {"id": "stop", "label": "中止する"},
                ],
                default="review_policy",
            )
        )
    elif identity.get("policy") == "not_configured" and intent in {
        "push",
        "open_pr",
        "publish",
        "release",
    }:
        proposals.append(
            _proposal(
                id="configure_expected_identity",
                kind="optional_policy",
                severity="recommended",
                question=(
                    "作者名義の固定照合が未設定です。"
                    "公開名義を指定して履歴を照合しますか?"
                ),
                current=None,
                proposed={"action": "set_expected_identity_and_rescan"},
                options=_yes_no_options(
                    "名義を指定して照合する",
                    "今は照合せず続ける",
                ),
                default="yes" if intent in {"publish", "release"} else "no",
                blocks_intent=False,
            )
        )

    origin = checks.get("origin") or {}
    if origin.get("status") != "pass" and intent in {
        "push",
        "open_pr",
        "merge",
        "publish",
        "release",
    }:
        proposals.append(
            _proposal(
                id="configure_origin",
                kind="missing_remote",
                severity="required",
                question=(
                    "origin remote がありません。"
                    "private remote を追加してから進めますか?"
                ),
                current=None,
                proposed={"action": "add_private_origin_remote"},
                options=_yes_no_options("remote を設定する", "中止する"),
                default="yes",
            )
        )

    dep_audit = checks.get("dependency_vulnerability_audit") or {}
    if dep_audit.get("status") == "unknown" and intent in {"publish", "release"}:
        proposals.append(
            _proposal(
                id="run_dependency_audit",
                kind="external_evidence",
                severity="recommended",
                question=(
                    "依存ライブラリの脆弱性監査は CLI 範囲外です。"
                    "エコシステム固有の監査を別途実行した扱いにしますか?"
                    "（未実施のまま公開する場合は no）"
                ),
                current="unknown",
                proposed={"action": "record_external_dependency_audit_evidence"},
                options=[
                    {"id": "done", "label": "監査済みなので記録する"},
                    {"id": "later", "label": "未実施。公開判断では未確認のまま残す"},
                ],
                default="later",
                blocks_intent=False,
            )
        )

    ci_runtime = checks.get("ci_runtime_result") or {}
    if ci_runtime.get("status") == "unknown" and intent in {
        "open_pr",
        "merge",
        "publish",
        "release",
    }:
        proposals.append(
            _proposal(
                id="confirm_ci_runtime",
                kind="external_evidence",
                severity="recommended" if intent == "open_pr" else "required",
                question=(
                    "remote CI の成功は未確認です。"
                    f"{INTENT_LABELS.get(intent, intent)} の前に現在の CI 結果を確認しますか?"
                ),
                current="unknown",
                proposed={"action": "fetch_current_remote_ci_evidence"},
                options=_yes_no_options(
                    "CI を確認する", "未確認のまま進める判断をする"
                ),
                default="yes",
                blocks_intent=intent in {"merge", "publish", "release"},
            )
        )

    readme = checks.get("readme_release_design")
    if isinstance(readme, dict) and readme.get("status") == "fail":
        proposals.append(
            _proposal(
                id="fix_readme_release_design",
                kind="readme_design",
                severity="required",
                question=(
                    "README の情報設計ゲートが fail です。"
                    "不足箇所を直してから release / 公開準備を続けますか?"
                ),
                current={
                    "design_status": readme.get("design_status"),
                    "findings": readme.get("findings"),
                    "recommended_capabilities": readme.get("recommended_capabilities"),
                },
                proposed={"action": "improve_readme_then_rerun_release_gate"},
                options=_yes_no_options("直して再検査", "中止する"),
                default="yes",
            )
        )

    if intent == "publish" and audience in {"unspecified", "local"}:
        proposals.append(
            _proposal(
                id="choose_audience",
                kind="audience",
                severity="required",
                question=(
                    "見せる相手 (audience) が未確定です。"
                    "Web全体 / team / 客先 / 外部協力者のどれに広げますか?"
                ),
                current=audience,
                proposed={"action": "set_audience_before_expansion"},
                options=[
                    {"id": "public", "label": "Web全体 (public化)"},
                    {"id": "team", "label": "team / organization"},
                    {"id": "client", "label": "客先納品"},
                    {"id": "collaborator", "label": "外部協力者"},
                    {"id": "stop", "label": "今は広げない"},
                ],
                default="stop",
            )
        )

    if intent == "create_repo":
        # create_repo は scan がなくても呼ぶが、scan がある場合は文書不足などを上に載せた
        proposals.extend(build_create_repo_proposals(scan_available=status is not None))

    return proposals


def build_create_repo_proposals(*, scan_available: bool) -> list[dict[str, Any]]:
    proposals = [
        _proposal(
            id="confirm_visibility_private_default",
            kind="github_settings",
            severity="required",
            question=(
                "新しい GitHub リポジトリは private で作成しますか?"
                "（default は private。public は別承認）"
            ),
            current=None,
            proposed={"visibility": "private"},
            options=[
                {"id": "private", "label": "private で作成する (推奨)"},
                {"id": "stop", "label": "作成しない"},
            ],
            default="private",
            why="新規 repo の default は private。public 作成は publish intent へ分離",
        ),
        _proposal(
            id="confirm_repo_identity",
            kind="github_settings",
            severity="required",
            question=(
                "作成する owner/name と使用アカウントを確定しますか?"
                "（曖昧なまま作成しません）"
            ),
            current=None,
            proposed={"action": "confirm_owner_name_and_account"},
            options=_yes_no_options("owner/name と account を確定する", "中止する"),
            default="yes",
        ),
        _proposal(
            id="seed_required_documents",
            kind="scaffold",
            severity="recommended",
            question=(
                "README / LICENSE / SECURITY / CONTRIBUTING / PREFLIGHT を"
                "作成時に揃えますか?"
            ),
            current="missing_or_unknown" if not scan_available else "see_scan",
            proposed={"action": "seed_required_documents_from_templates"},
            options=_yes_no_options("必須文書を揃える", "後で揃える"),
            default="yes",
            blocks_intent=False,
        ),
    ]
    return proposals


def build_github_settings_proposals(
    review: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """read-only settings review を設定ごとの対話提案へ変換する。"""
    if review is None:
        return [
            _proposal(
                id="inspect_github_settings",
                kind="github_setting_change",
                severity="required",
                question=(
                    "GitHub Settings のread-only取得結果がありません。"
                    "対象owner/nameと認証を確認して再検査しますか?"
                ),
                current="unknown",
                proposed={"action": "inspect_github_settings_then_rerun"},
                options=_yes_no_options("再検査する", "中止する"),
                default="yes",
                why="取得不能を無効(false)と推測しない",
            )
        ]

    proposals: list[dict[str, Any]] = []
    if review.get("status") == "needs_human_input" and not review.get("settings"):
        return [
            _proposal(
                id="inspect_github_settings",
                kind="github_setting_change",
                severity="required",
                question=(
                    "GitHub owner/nameまたは認証accountを解決できず、Settingsを"
                    "一件も確認できませんでした。取得条件を直して再検査しますか?"
                ),
                current={
                    "repository": review.get("repository"),
                    "unknowns": review.get("unknowns") or [],
                },
                proposed={"action": "inspect_github_settings_then_rerun"},
                options=_yes_no_options("再検査する", "中止する"),
                default="yes",
                blocks_intent=True,
                why="未取得を設定適合と誤判定しない",
            )
        ]
    for setting in review.get("settings") or []:
        if setting.get("classification") == "no_change":
            continue
        name = str(setting.get("name") or "unknown")
        safe_name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
        tier = str(setting.get("tier") or "recommended")
        unavailable = setting.get("classification") == "unavailable"
        if name == "authenticated_account":
            proposals.append(
                _proposal(
                    id="github_setting_authenticated_account",
                    kind="github_account_confirmation",
                    severity="required",
                    question=(
                        f"GitHub認証accountは {setting.get('observed_value')!r} です。"
                        "このaccountを対象repositoryの設定確認に使うことを確認しますか?"
                    ),
                    current={
                        "repository": review.get("repository"),
                        "login": setting.get("observed_value"),
                        "classification": setting.get("classification"),
                    },
                    proposed={
                        "approved": False,
                        "action": "confirm_authenticated_account_then_reinspect",
                        "expected": setting.get("recommended_value"),
                    },
                    options=_yes_no_options("accountを確認する", "中止する"),
                    default="yes",
                    blocks_intent=bool(setting.get("blocks_intent")),
                    why=str(setting.get("reason") or "acting accountを固定する"),
                )
            )
            continue
        proposals.append(
            _proposal(
                id=f"github_setting_{safe_name or 'unknown'}",
                kind="github_setting_change",
                severity="required" if tier == "required" else "recommended",
                question=(
                    f"GitHub setting `{name}` は current={setting.get('observed_value')!r}, "
                    f"recommended={setting.get('recommended_value')!r} です。"
                    + (
                        "取得不能のため権限・plan・organization policyを確認して再検査しますか?"
                        if unavailable
                        else "外部影響とrollbackを確認し、この設定だけ変更候補にしますか?"
                    )
                ),
                current={
                    "repository": review.get("repository"),
                    "profile": review.get("profile"),
                    "value": setting.get("observed_value"),
                    "classification": setting.get("classification"),
                },
                proposed={
                    "approved": False,
                    "recommended_value": setting.get("recommended_value"),
                    "operation": setting.get("proposed_operation"),
                    "external_effect": setting.get("external_effect"),
                    "rollback": setting.get("rollback"),
                    "on_approval": "fresh_read_then_execute_separately_then_verify",
                },
                options=(
                    [
                        {"id": "reinspect", "label": "取得条件を直して再検査"},
                        {"id": "stop", "label": "中止する"},
                    ]
                    if unavailable
                    else [
                        {"id": "previewed", "label": "個別変更の承認判断へ進む"},
                        {"id": "keep", "label": "現在値を維持する"},
                    ]
                ),
                default="reinspect" if unavailable else "keep",
                blocks_intent=bool(setting.get("blocks_intent")),
                why=str(setting.get("reason") or "GitHub Settings profileとの差分"),
            )
        )
    return proposals


def build_confirmations(
    *,
    intent: str,
    audience: str,
    scan: dict[str, Any] | None,
    github_settings_review: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    label = INTENT_LABELS.get(intent, intent)
    head = (scan or {}).get("head")
    repo = (scan or {}).get("repo") or (github_settings_review or {}).get("repository")
    scan_scope = (scan or {}).get("scan_scope") or {}
    base_ref = scan_scope.get("base_ref")
    base_oid = scan_scope.get("base_oid")
    consistency_scope = (scan or {}).get("consistency_scope") or {}
    consistency_base_ref = consistency_scope.get("base_ref")
    consistency_base_oid = consistency_scope.get("base_oid")
    base = {
        "id": f"confirm_{intent}",
        "kind": "intent_confirmation",
        "severity": "required",
        "question": (
            f"次の操作へ進んでよいですか: {label}"
            + (f" / repo={repo}" if repo else "")
            + (f" / head={head}" if head else "")
            + (f" / base={base_ref}@{base_oid}" if base_ref and base_oid else "")
            + (
                f" / consistency_base={consistency_base_ref}@{consistency_base_oid}"
                if consistency_base_ref and consistency_base_oid
                else ""
            )
            + (f" / audience={audience}" if intent == "publish" else "")
            + "。実行内容を再掲したうえで yes が必要です。"
        ),
        "options": [
            {"id": "approve", "label": "内容を確認したのでこの intent の準備を進める"},
            {"id": "cancel", "label": "キャンセル"},
        ],
        "default": "cancel",
        "blocks_intent": True,
        "auto_apply_safe": False,
    }
    if intent == "publish":
        base["proposed"] = {
            "action": "expand_audience",
            "reminder": (
                "visibility 変更・共有・納品は、この approve のあとも"
                "正確な操作文面で再承認する"
            ),
        }
    elif intent == "create_repo":
        base["proposed"] = {
            "action": "create_private_repository",
            "reminder": "public 作成や push は含めない",
        }
    elif intent == "configure_settings":
        base["proposed"] = {
            "action": "configure_settings",
            "reminder": (
                "この確認は設定変更を包括承認しない。対象repository、現在値、"
                "正確な操作、外部影響、rollbackを設定ごとに再掲して別承認する"
            ),
        }
    else:
        base["proposed"] = {"action": intent}
        if base_ref and base_oid:
            base["proposed"]["operation_binding"] = {
                "base_ref": base_ref,
                "base_oid": base_oid,
                "head_oid": head,
                "require_exact_base_for_push_or_pr": True,
                "rerun_if_base_or_head_changes": True,
            }
    if consistency_base_ref and consistency_base_oid:
        base["proposed"]["consistency_binding"] = {
            "base_ref": consistency_base_ref,
            "base_oid": consistency_base_oid,
            "head_oid": head,
            "scope": "repository_consistency_only",
            "rerun_if_base_or_head_changes": True,
        }
    return [base]


def dialogue_status(
    *,
    scan: dict[str, Any] | None,
    proposals: list[dict[str, Any]],
) -> str:
    if scan and scan.get("status") == "tool_error":
        return "blocked"
    blocking = [
        p
        for p in proposals
        if p.get("blocks_intent") and p.get("severity") in {"required", "error"}
    ]
    # security holds は常に blocked（回答前）
    hold_kinds = {"security_hold", "privacy_hold", "tool_error"}
    if any(p.get("kind") in hold_kinds for p in proposals):
        return "blocked"
    if blocking:
        return "needs_human_input"
    if scan and scan.get("status") == "blocked":
        return "needs_human_input"
    return "ready_after_confirmation"


def build_github_baseline_proposal(
    baseline_status: dict[str, Any],
) -> dict[str, Any] | None:
    if baseline_status.get("status") != "stale":
        return None
    return _proposal(
        id="refresh_github_settings_baseline",
        kind="github_baseline",
        severity="recommended",
        question=(
            f"同梱の GitHub 設定ガイド (last_reviewed={baseline_status.get('last_reviewed')}, "
            f"age={baseline_status.get('age_days')}日) が鮮度期限 "
            f"({baseline_status.get('max_age_days')}日) を超えています。"
            "公式ドキュメント / changelog を確認してガイドを更新しますか?"
        ),
        current=baseline_status,
        proposed={
            "action": "review_and_update_github_settings_guide",
            "document": "references/github-settings.md",
            "on_done": "bump last_reviewed in baseline marker",
        },
        options=[
            {"id": "review_now", "label": "今すぐガイドを見直して更新する"},
            {
                "id": "ack_current",
                "label": "現状のガイドで進め、期限を延ばす記録だけする",
            },
            {"id": "later", "label": "後で対応する (今回は進める)"},
        ],
        default="later",
        blocks_intent=False,
        why=(
            "GitHub 製品変更のリアルタイム自動追従は保証しない。"
            "代わりに last_reviewed 期限切れを検知して更新確認を出す。"
        ),
    )


def build_dialogue(
    *,
    intent: str,
    scan: dict[str, Any] | None = None,
    audience: str = "unspecified",
    guarantees: list[str] | None = None,
    non_guarantees: list[str] | None = None,
    preferences: dict[str, Any] | None = None,
    github_baseline: dict[str, Any] | None = None,
    preferences_module: Any | None = None,
    github_settings_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if intent not in INTENTS:
        raise ValueError(f"unknown intent: {intent}")

    # preferences / github baseline は呼び出し側から渡す (import 循環を避ける)
    prefs = preferences if preferences is not None else {"dismissals": {}}

    proposals: list[dict[str, Any]] = []
    if intent == "create_repo" and scan is None:
        proposals.extend(build_create_repo_proposals(scan_available=False))
    elif intent == "configure_settings":
        # remote Settings review はlocal repository scanと独立したread-only gate。
        # repo pathはorigin解決にだけ使い、既存local baselineを設定確認へ混ぜない。
        pass
    elif scan is not None:
        proposals.extend(
            build_proposals_from_scan(intent=intent, scan=scan, audience=audience)
        )
    else:
        proposals.append(
            _proposal(
                id="run_scan_first",
                kind="missing_scan",
                severity="required",
                question=(
                    f"{INTENT_LABELS[intent]} の前に readiness_scan を実行しますか?"
                ),
                current=None,
                proposed={"action": "run_readiness_scan"},
                options=_yes_no_options("検査する", "中止する"),
                default="yes",
            )
        )

    if intent == "configure_settings":
        proposals.extend(build_github_settings_proposals(github_settings_review))

    if github_baseline is not None:
        baseline_proposal = build_github_baseline_proposal(github_baseline)
        if baseline_proposal is not None and intent in {
            "create_repo",
            "publish",
            "release",
            "open_pr",
            "merge",
            "configure_settings",
        }:
            proposals.append(baseline_proposal)

    suppressed: list[dict[str, Any]] = []
    if preferences_module is not None:
        proposals = preferences_module.apply_dismissal_options(proposals)
        proposals, suppressed = preferences_module.filter_dismissed_proposals(
            proposals, prefs
        )
    else:
        for proposal in proposals:
            proposal["dismissible"] = False
            proposal["max_dismissal"] = None

    # create_repo + scan 時は build_proposals_from_scan 内で create 提案も付く
    confirmations = build_confirmations(
        intent=intent,
        audience=audience,
        scan=scan,
        github_settings_review=github_settings_review,
    )
    # intent 最終確認自体は dismiss 不可
    for item in confirmations:
        item["dismissible"] = False
        item["max_dismissal"] = None

    status = dialogue_status(scan=scan, proposals=proposals)

    return {
        "schema": DIALOGUE_SCHEMA,
        "intent": intent,
        "intent_label": INTENT_LABELS[intent],
        "status": status,
        "publication_decision": "blocked_human_review_required",
        "audience": audience,
        "guarantees": list(guarantees or ()),
        "non_guarantees": list(non_guarantees or ()),
        "scan": scan,
        "preferences": {
            "schema": prefs.get("schema"),
            "path": ".repo-preflight.json",
            "active_dismissal_count": len(suppressed),
            "load_error": prefs.get("load_error"),
        },
        "github_baseline": github_baseline,
        "github_settings_review": github_settings_review,
        "proposals": proposals,
        "suppressed_proposals": suppressed,
        "confirmations": confirmations,
        "agent_instructions": list(AGENT_INSTRUCTIONS),
        "next_step": (
            "ユーザーに proposals / confirmations を提示し、回答を得てから intent を続行する。"
            "dismiss_* 回答は .repo-preflight.json に記録する"
            if status != "ready_after_confirmation"
            else "confirmations の approve を取ったうえで intent を実行する。"
            "実行後は verify し、結果を報告する"
        ),
    }


def format_dialogue_for_agent(dialogue: dict[str, Any]) -> str:
    """エージェントがユーザーへ転記しやすいテキスト。"""
    lines = [
        f"# Repo Preflight 対話ゲート — {dialogue.get('intent_label')}",
        "",
        f"intent: {dialogue.get('intent')}",
        f"status: {dialogue.get('status')}",
        f"publication_decision: {dialogue.get('publication_decision')}",
        "",
        "## 保証すること",
        *(f"- {item}" for item in dialogue.get("guarantees") or []),
        "",
        "## 保証しないこと",
        *(f"- {item}" for item in dialogue.get("non_guarantees") or []),
        "",
        "## 確認が必要な設定・不足",
    ]
    proposals = dialogue.get("proposals") or []
    if not proposals:
        lines.append("- (必須の不足提案はありません。最終確認へ進んでください)")
    for index, item in enumerate(proposals, start=1):
        dismiss_tag = (
            f" dismissible={item.get('max_dismissal')}"
            if item.get("dismissible")
            else " dismissible=no"
        )
        lines.append(
            f"{index}. [{item.get('severity')}]{dismiss_tag} {item.get('question')}"
        )
        lines.append(f"   id: {item.get('id')}")
        if item.get("current") is not None:
            lines.append(f"   current: {item.get('current')}")
        lines.append(f"   proposed: {item.get('proposed')}")
        option_text = ", ".join(
            f"{opt.get('id')}={opt.get('label')}" for opt in item.get("options") or []
        )
        lines.append(f"   options: {option_text}")
        lines.append(f"   default: {item.get('default')}")
    suppressed = dialogue.get("suppressed_proposals") or []
    if suppressed:
        lines.extend(["", "## 次から出さない設定で抑止中"])
        for item in suppressed:
            lines.append(f"- {item.get('id')}: {item.get('dismissal')}")
    baseline = dialogue.get("github_baseline") or {}
    if baseline:
        lines.extend(
            [
                "",
                "## GitHub 設定ガイド鮮度",
                f"- status: {baseline.get('status')}",
                f"- last_reviewed: {baseline.get('last_reviewed')}",
                f"- age_days: {baseline.get('age_days')}",
                f"- max_age_days: {baseline.get('max_age_days')}",
            ]
        )
    lines.extend(["", "## 最終確認"])
    for index, item in enumerate(dialogue.get("confirmations") or [], start=1):
        lines.append(f"{index}. {item.get('question')}")
        lines.append(f"   default: {item.get('default')}")
    lines.extend(
        [
            "",
            "## エージェント向け",
            *(f"- {item}" for item in dialogue.get("agent_instructions") or []),
            "",
            f"next_step: {dialogue.get('next_step')}",
        ]
    )
    return "\n".join(lines)


def intent_needs_scan(intent: str) -> bool:
    return intent not in {"create_repo", "configure_settings"}


def intent_uses_release_gate(intent: str) -> bool:
    return _intent_release_mode(intent)

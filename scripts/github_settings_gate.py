"""GitHub repository Settings の読み取り専用 review gate。

設定変更は行わず、inspect -> compare -> preview までを機械可読 packet にする。
404/403/plan 制約は false と推測せず unavailable として扱う。
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


SCHEMA_VERSION = "repo-preflight.github-settings-review/v1"
PROFILES = ("solo_public", "team_public", "high_risk_public")


class ApiUnavailable(RuntimeError):
    def __init__(self, *, status_code: int | None, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def repository_from_remote(remote: str) -> str | None:
    """GitHub.com の remote URL だけを OWNER/REPO に正規化する。"""
    value = remote.strip().split("?", 1)[0].split("#", 1)[0]
    scp = re.match(r"^(?:[^@/:]+@)?([^/:]+):(.+)$", value)
    if "://" not in value and scp:
        host, path = scp.groups()
    else:
        parts = urlsplit(value)
        host = parts.hostname or ""
        path = parts.path
    if host.lower() != "github.com":
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    pieces = path.split("/")
    if len(pieces) != 2 or not all(pieces):
        return None
    return f"{pieces[0]}/{pieces[1]}"


def repository_from_repo(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        capture_output=True,
        shell=False,
    )
    if result.returncode != 0:
        return None
    return repository_from_remote(result.stdout)


def gh_api_get(endpoint: str) -> Any:
    """`gh api` を GET 専用で実行する。stderr の本文は packet に転記しない。"""
    try:
        result = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            capture_output=True,
            shell=False,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise ApiUnavailable(status_code=None, reason="gh_cli_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise ApiUnavailable(status_code=None, reason="github_api_timeout") from exc
    if result.returncode != 0:
        status_match = re.search(r"HTTP\s+(\d{3})", result.stderr)
        status_code = int(status_match.group(1)) if status_match else None
        reason = (
            "not_found_or_plan_unavailable"
            if status_code == 404
            else (
                "forbidden_or_plan_unavailable"
                if status_code == 403
                else (
                    "authentication_required"
                    if status_code == 401
                    else "github_api_unavailable"
                )
            )
        )
        raise ApiUnavailable(status_code=status_code, reason=reason)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApiUnavailable(
            status_code=None, reason="invalid_github_api_json"
        ) from exc


def _operation(
    method: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"method": method, "endpoint": endpoint}
    if body is not None:
        result["body"] = body
    return result


def _setting(
    *,
    name: str,
    tier: str,
    observed: Any,
    recommended: Any,
    source: str,
    reason: str,
    effect: str,
    proposed_operation: dict[str, Any] | None,
    rollback: dict[str, Any] | str | None,
    matches: bool | None,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    if matches is True:
        classification = "no_change"
    elif matches is None:
        classification = "unavailable"
    elif tier == "required":
        classification = "human_decision"
    else:
        classification = "recommended_change"
    return {
        "name": name,
        "tier": tier,
        "observed_value": observed if matches is not None else "unknown",
        "recommended_value": recommended,
        "classification": classification,
        "reason": reason,
        "source_endpoint": source,
        "unavailable_reason": unavailable_reason,
        "external_effect": effect,
        "proposed_operation": proposed_operation if matches is False else None,
        "rollback": rollback if matches is False else None,
        "approved": False,
        "blocks_intent": tier == "required" and matches is not True,
    }


def _fetch(
    api_get: Callable[[str], Any], endpoint: str
) -> tuple[Any, ApiUnavailable | None]:
    try:
        return api_get(endpoint), None
    except ApiUnavailable as exc:
        return None, exc
    except Exception as exc:  # injection/runtime error details may contain secrets
        return None, ApiUnavailable(
            status_code=None, reason=f"api_reader_error:{type(exc).__name__}"
        )


def _simple_setting(
    *,
    endpoint: str,
    data: dict[str, Any] | None,
    error: ApiUnavailable | None,
    field: str,
    name: str,
    tier: str,
    recommended: Any,
    method: str,
    reason: str,
    effect: str,
    body: dict[str, Any] | None = None,
    rollback_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if error or data is None or field not in data:
        unavailable_reason = error.reason if error else "field_not_returned"
        return _setting(
            name=name,
            tier=tier,
            observed=None,
            recommended=recommended,
            source=endpoint,
            reason=reason,
            effect=effect,
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason=unavailable_reason,
        )
    observed = data[field]
    desired_body = body if body is not None else {field: recommended}
    old_body = rollback_body if rollback_body is not None else {field: observed}
    return _setting(
        name=name,
        tier=tier,
        observed=observed,
        recommended=recommended,
        source=endpoint,
        reason=reason,
        effect=effect,
        proposed_operation=_operation(method, endpoint, desired_body),
        rollback=_operation(method, endpoint, old_body),
        matches=observed == recommended,
    )


def _security_setting(
    *,
    repository: str,
    root: dict[str, Any] | None,
    error: ApiUnavailable | None,
    name: str,
    tier: str,
    reason: str,
    effect: str,
) -> dict[str, Any]:
    endpoint = f"repos/{repository}"
    if error:
        return _setting(
            name=name,
            tier=tier,
            observed=None,
            recommended="enabled",
            source=endpoint,
            reason=reason,
            effect=effect,
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason=error.reason,
        )
    security = (root or {}).get("security_and_analysis") or {}
    observed = (security.get(name) or {}).get("status")
    if observed not in {"enabled", "disabled"}:
        return _setting(
            name=name,
            tier=tier,
            observed=None,
            recommended="enabled",
            source=endpoint,
            reason=reason,
            effect=effect,
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason="security_setting_not_returned",
        )
    body = {"security_and_analysis": {name: {"status": "enabled"}}}
    rollback = {"security_and_analysis": {name: {"status": observed}}}
    return _setting(
        name=name,
        tier=tier,
        observed=observed,
        recommended="enabled",
        source=endpoint,
        reason=reason,
        effect=effect,
        proposed_operation=_operation("PATCH", endpoint, body),
        rollback=_operation("PATCH", endpoint, rollback),
        matches=observed == "enabled",
    )


def _ruleset_observations(
    repository: str,
    profile: str,
    api_get: Callable[[str], Any],
) -> list[dict[str, Any]]:
    endpoint = f"repos/{repository}/rulesets"
    summaries, list_error = _fetch(api_get, endpoint)
    names = (
        "default_branch_ruleset",
        "ruleset_deletion_protection",
        "ruleset_non_fast_forward_protection",
        "ruleset_pull_request",
        "required_review_thread_resolution",
        "required_status_checks",
        "strict_required_status_checks_policy",
        "required_approving_review_count",
    )
    if list_error:
        return [
            _setting(
                name=name,
                tier="required",
                observed=None,
                recommended=(
                    {"minimum": 1}
                    if name == "required_approving_review_count"
                    and profile != "solo_public"
                    else True
                ),
                source=endpoint,
                reason="default branch の変更経路を ruleset で固定する",
                effect="default branch の直接変更・削除・未検証mergeを制限",
                proposed_operation=None,
                rollback=None,
                matches=None,
                unavailable_reason=list_error.reason,
            )
            for name in names
        ]

    details: list[dict[str, Any]] = []
    detail_error: ApiUnavailable | None = None
    for summary in summaries or []:
        ruleset_id = summary.get("id") if isinstance(summary, dict) else None
        if ruleset_id is None:
            continue
        detail, error = _fetch(api_get, f"repos/{repository}/rulesets/{ruleset_id}")
        if error:
            detail_error = error
            continue
        if isinstance(detail, dict):
            details.append(detail)
    candidates = [
        item
        for item in details
        if item.get("target") == "branch"
        and item.get("enforcement") == "active"
        and "~DEFAULT_BRANCH"
        in (((item.get("conditions") or {}).get("ref_name") or {}).get("include") or [])
    ]
    if not candidates:
        reason = detail_error.reason if detail_error else None
        matches: bool | None = None if detail_error and not details else False
        return [
            _setting(
                name="default_branch_ruleset",
                tier="required",
                observed=None,
                recommended="active_ruleset_for_default_branch",
                source=endpoint,
                reason="default branch を対象にした active ruleset が必要",
                effect="default branch の変更経路を制限",
                proposed_operation=None,
                rollback=None,
                matches=matches,
                unavailable_reason=reason,
            )
        ]

    ruleset = candidates[0]
    ruleset_endpoint = f"repos/{repository}/rulesets/{ruleset['id']}"
    rules = {rule.get("type"): rule for rule in ruleset.get("rules") or []}
    pr_params = (rules.get("pull_request") or {}).get("parameters") or {}
    check_params = (rules.get("required_status_checks") or {}).get("parameters") or {}
    current_body = {
        key: ruleset.get(key)
        for key in (
            "name",
            "target",
            "enforcement",
            "bypass_actors",
            "conditions",
            "rules",
        )
    }

    def ruleset_setting(
        name: str,
        observed: Any,
        recommended: Any,
        matches: bool,
        reason: str,
    ) -> dict[str, Any]:
        return _setting(
            name=name,
            tier="required",
            observed=observed,
            recommended=recommended,
            source=ruleset_endpoint,
            reason=reason,
            effect="default branch の更新条件を変更",
            proposed_operation={
                "method": "PUT",
                "endpoint": ruleset_endpoint,
                "body_basis": "fresh_ruleset_body_required",
                "change": {name: recommended},
            },
            rollback=_operation("PUT", ruleset_endpoint, current_body),
            matches=matches,
        )

    approvals = int(pr_params.get("required_approving_review_count") or 0)
    minimum = 0 if profile == "solo_public" else 1
    required_checks = check_params.get("required_status_checks") or []
    return [
        ruleset_setting(
            "default_branch_ruleset",
            "active",
            "active_ruleset_for_default_branch",
            True,
            "default branch を active ruleset で保護する",
        ),
        ruleset_setting(
            "ruleset_deletion_protection",
            "deletion" in rules,
            True,
            "deletion" in rules,
            "default branch の削除を禁止する",
        ),
        ruleset_setting(
            "ruleset_non_fast_forward_protection",
            "non_fast_forward" in rules,
            True,
            "non_fast_forward" in rules,
            "force push を禁止する",
        ),
        ruleset_setting(
            "ruleset_pull_request",
            "pull_request" in rules,
            True,
            "pull_request" in rules,
            "default branch への変更をPR経由にする",
        ),
        ruleset_setting(
            "required_review_thread_resolution",
            pr_params.get("required_review_thread_resolution"),
            True,
            pr_params.get("required_review_thread_resolution") is True,
            "未解決review threadを残したmergeを防ぐ",
        ),
        ruleset_setting(
            "required_status_checks",
            [item.get("context") for item in required_checks],
            "one_or_more_current_required_checks",
            bool(required_checks),
            "required check が空の保護を防ぐ",
        ),
        ruleset_setting(
            "strict_required_status_checks_policy",
            check_params.get("strict_required_status_checks_policy"),
            True,
            check_params.get("strict_required_status_checks_policy") is True,
            "最新baseとの整合を確認してからmergeする",
        ),
        ruleset_setting(
            "required_approving_review_count",
            approvals,
            {"minimum": minimum},
            approvals >= minimum,
            "maintainer人数に応じたreview承認数を要求する",
        ),
    ]


def review_repository(
    repository: str,
    profile: str,
    *,
    api_get: Callable[[str], Any] = gh_api_get,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/REPO")

    root_endpoint = f"repos/{repository}"
    root, root_error = _fetch(api_get, root_endpoint)
    if (
        root_error is None
        and isinstance(root, dict)
        and str(root.get("full_name") or "").lower() != repository.lower()
    ):
        root_error = ApiUnavailable(
            status_code=None, reason="repository_identity_mismatch"
        )
    actions_endpoint = f"repos/{repository}/actions/permissions"
    actions, actions_error = _fetch(api_get, actions_endpoint)
    workflow_endpoint = f"repos/{repository}/actions/permissions/workflow"
    workflow, workflow_error = _fetch(api_get, workflow_endpoint)

    settings: list[dict[str, Any]] = []
    settings.append(
        _simple_setting(
            endpoint=root_endpoint,
            data=root,
            error=root_error,
            field="delete_branch_on_merge",
            name="delete_branch_on_merge",
            tier="recommended",
            recommended=True,
            method="PATCH",
            reason="merge済み短期branchを自動整理する",
            effect="今後mergeしたremote head branchを自動削除",
        )
    )
    for field, recommended in (
        ("allow_squash_merge", True),
        ("allow_merge_commit", False),
        ("allow_rebase_merge", False),
        ("allow_auto_merge", False),
    ):
        settings.append(
            _simple_setting(
                endpoint=root_endpoint,
                data=root,
                error=root_error,
                field=field,
                name=field,
                tier="recommended",
                recommended=recommended,
                method="PATCH",
                reason="小さなversion幅と追跡しやすい履歴を維持する",
                effect="GitHub UIで選べるmerge方法を変更",
            )
        )

    settings.extend(
        [
            _simple_setting(
                endpoint=actions_endpoint,
                data=actions,
                error=actions_error,
                field="enabled",
                name="actions_enabled",
                tier="required",
                recommended=True,
                method="PUT",
                reason="required CIを実行可能にする",
                effect="repositoryのGitHub Actions実行可否を変更",
            ),
            _simple_setting(
                endpoint=actions_endpoint,
                data=actions,
                error=actions_error,
                field="sha_pinning_required",
                name="sha_pinning_required",
                tier="required" if profile == "high_risk_public" else "recommended",
                recommended=True,
                method="PUT",
                reason="workflow action参照の供給網リスクを抑える",
                effect="full commit SHAでないaction参照を拒否",
            ),
            _simple_setting(
                endpoint=actions_endpoint,
                data=actions,
                error=actions_error,
                field="allowed_actions",
                name="allowed_actions",
                tier="required" if profile == "high_risk_public" else "recommended",
                recommended="selected",
                method="PUT",
                reason="実行可能な第三者actionを明示的に制限する",
                effect="許可list外のaction実行を拒否",
            ),
            _simple_setting(
                endpoint=workflow_endpoint,
                data=workflow,
                error=workflow_error,
                field="default_workflow_permissions",
                name="default_workflow_permissions",
                tier="required",
                recommended="read",
                method="PUT",
                reason="GITHUB_TOKENの既定権限を最小化する",
                effect="明示permissionsのないworkflow tokenをread-only化",
            ),
            _simple_setting(
                endpoint=workflow_endpoint,
                data=workflow,
                error=workflow_error,
                field="can_approve_pull_request_reviews",
                name="can_approve_pull_request_reviews",
                tier="required",
                recommended=False,
                method="PUT",
                reason="workflow自身によるreview承認を禁止する",
                effect="GitHub ActionsからのPR review承認を禁止",
            ),
        ]
    )

    if not actions_error and (actions or {}).get("allowed_actions") == "selected":
        selected_endpoint = f"repos/{repository}/actions/permissions/selected-actions"
        selected, selected_error = _fetch(api_get, selected_endpoint)
        settings.extend(
            [
                _simple_setting(
                    endpoint=selected_endpoint,
                    data=selected,
                    error=selected_error,
                    field="github_owned_allowed",
                    name="selected_actions_github_owned_allowed",
                    tier="required",
                    recommended=True,
                    method="PUT",
                    reason="CIで利用するGitHub公式actionを許可する",
                    effect="GitHub-owned actionの実行可否を変更",
                ),
                _simple_setting(
                    endpoint=selected_endpoint,
                    data=selected,
                    error=selected_error,
                    field="verified_allowed",
                    name="selected_actions_verified_allowed",
                    tier=(
                        "required" if profile == "high_risk_public" else "recommended"
                    ),
                    recommended=False,
                    method="PUT",
                    reason="verified publisher全体ではなく必要なactionだけを許可する",
                    effect="Marketplace verified creatorのaction一括許可を無効化",
                ),
            ]
        )
        if selected_error:
            settings.append(
                _setting(
                    name="selected_actions_patterns",
                    tier="required" if profile == "high_risk_public" else "recommended",
                    observed=None,
                    recommended="repository_specific_least_privilege",
                    source=selected_endpoint,
                    reason="selected policyの許可list詳細まで確認する",
                    effect="許可list外actionの実行可否に影響",
                    proposed_operation=None,
                    rollback=None,
                    matches=None,
                    unavailable_reason=selected_error.reason,
                )
            )
        else:
            settings.append(
                _setting(
                    name="selected_actions_patterns",
                    tier="optional",
                    observed=(selected or {}).get("patterns_allowed") or [],
                    recommended="repository_specific_least_privilege",
                    source=selected_endpoint,
                    reason="workflowで使う第三者actionだけを許可する",
                    effect="許可list外actionの実行可否に影響",
                    proposed_operation=None,
                    rollback=None,
                    matches=True,
                )
            )

    settings.extend(_ruleset_observations(repository, profile, api_get))
    for name, reason, effect in (
        (
            "dependabot_security_updates",
            "脆弱な依存更新を追跡する",
            "security update PRを有効化",
        ),
        (
            "secret_scanning",
            "公開履歴のsecret候補を検出する",
            "secret scanningを有効化",
        ),
        (
            "secret_scanning_push_protection",
            "secretの新規pushを入口で止める",
            "検出されたsecretを含むpushを拒否",
        ),
    ):
        settings.append(
            _security_setting(
                repository=repository,
                root=root,
                error=root_error,
                name=name,
                tier="required",
                reason=reason,
                effect=effect,
            )
        )

    pvr_endpoint = f"repos/{repository}/private-vulnerability-reporting"
    pvr, pvr_error = _fetch(api_get, pvr_endpoint)
    pvr_tier = "required" if profile == "high_risk_public" else "recommended"
    if pvr_error or not isinstance(pvr, dict) or "enabled" not in pvr:
        settings.append(
            _setting(
                name="private_vulnerability_reporting",
                tier=pvr_tier,
                observed=None,
                recommended=True,
                source=pvr_endpoint,
                reason="公開issueを使わず脆弱性を受け付ける",
                effect="外部報告者が非公開で脆弱性を送信可能になる",
                proposed_operation=None,
                rollback=None,
                matches=None,
                unavailable_reason=(
                    pvr_error.reason if pvr_error else "field_not_returned"
                ),
            )
        )
    else:
        observed = bool((pvr or {}).get("enabled"))
        settings.append(
            _setting(
                name="private_vulnerability_reporting",
                tier=pvr_tier,
                observed=observed,
                recommended=True,
                source=pvr_endpoint,
                reason="公開issueを使わず脆弱性を受け付ける",
                effect="外部報告者が非公開で脆弱性を送信可能になる",
                proposed_operation=_operation("PUT", pvr_endpoint),
                rollback=_operation("DELETE", pvr_endpoint),
                matches=observed,
            )
        )

    codeql_endpoint = f"repos/{repository}/code-scanning/default-setup"
    codeql, codeql_error = _fetch(api_get, codeql_endpoint)
    settings.append(
        _simple_setting(
            endpoint=codeql_endpoint,
            data=codeql,
            error=codeql_error,
            field="state",
            name="code_scanning_default_setup",
            tier="required" if profile == "high_risk_public" else "recommended",
            recommended="configured",
            method="PATCH",
            reason="default branchとPRの静的解析を継続する",
            effect="CodeQL default setupを有効化",
        )
    )

    required_changes = [item for item in settings if item["blocks_intent"]]
    unknowns = [
        {
            "setting": item["name"],
            "source_endpoint": item["source_endpoint"],
            "reason": item["unavailable_reason"],
        }
        for item in settings
        if item["classification"] == "unavailable"
    ]
    timestamp = observed_at or datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "needs_human_input" if required_changes else "pass",
        "repository": repository,
        "profile": profile,
        "observed_at": timestamp.astimezone(timezone.utc).isoformat(),
        "settings": settings,
        "required_change_count": len(required_changes),
        "recommended_change_count": sum(
            item["classification"] == "recommended_change" for item in settings
        ),
        "unknowns": unknowns,
        "external_actions_performed": False,
        "next_step": (
            "設定ごとにpreviewし、現在会話で承認されたものだけ別工程で変更・再測定する"
        ),
    }

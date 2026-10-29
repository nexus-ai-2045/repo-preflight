#!/usr/bin/env python3
"""preflight-gate の呼び出し定義と、検査を変える PR を見る（base-guard）。

比較元（保護された側）の ref の `.github/workflows/*.yml`・`*.yaml` を `git show` で読む。
`--head-ref` を渡すと、PR 側の定義と、比較元から PR までに変わったパスも見る
（`pull_request_target` の別ワークフローから呼ぶ。PR のコードは実行せず、git の中身を読むだけ）。

赤にするもの:
- YAML として読めない定義（重複キー・アンカー・別名・タグも読めない扱い）。「導入前」にはしない
- preflight-gate を呼ぶ job の `uses` が `nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml`
  と完全一致しない（大文字小文字は区別しない）。別の owner・別名のリポ・手元の写しも赤
  （例外は repo-preflight 本体だけ: `--self-repository` が本体のときに限り、自分の
  `./.github/workflows/preflight-gate.yml` を `preflight-ref: ${{ github.sha }}` で呼ぶ定義を通す）
- `uses` の @ か `preflight-ref` が 40桁の commit SHA でない、欠けている、二つが違う
- 比較元に呼び出しが無い（`--allow-absent` のときだけ導入前として見送る）
- PR 側で呼び出しが消えた
- PR が保護したパス（同梱の references/protected-paths.json）を変えた

終了コード: 0 = 通す / 1 = 赤 / 2 = 読めない（git や PyYAML が無いなど）。
出力は種類・ファイル・job 名だけで、SHA の値は出さない。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

SCHEMA = "repo-preflight.base-guard/v2"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_TARGET = "nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml"
# repo-preflight 本体は PR の版の gate で自分を検査する。本体の中だけ、この形を写しではなく本物として扱う。
SELF_REPOSITORY = "nexus-ai-2045/repo-preflight"
SELF_TARGET = "./.github/workflows/preflight-gate.yml"
SELF_PREFLIGHT_REF = "${{ github.sha }}"
PROTECTED_PATHS = (
    Path(__file__).resolve().parent.parent / "references" / "protected-paths.json"
)

KINDS = {
    "unreadable": "workflow 定義を YAML として読めない",
    "foreign_caller": "preflight-gate の呼び出し先が固定の owner/リポと完全一致しない",
    "no_caller": "preflight-gate の呼び出しが無い",
    "caller_removed": "PR 側で preflight-gate の呼び出しが消えた",
    "uses_not_sha": "uses の @ が 40桁の commit SHA でない",
    "preflight_ref_missing": "with に preflight-ref が無い",
    "preflight_ref_not_sha": "preflight-ref が 40桁の commit SHA でない",
    "mismatch": "uses の @ と preflight-ref が違う SHA",
    "protected_path_changed": "検査を変えるパスが変わっている（人の確認が要る）",
    "verification_policy_changed": "承認では免除できない検査定義が変わっている",
    "preflight_ref_unreleased": "uses の SHA が repo-preflight の版のタグ（v*）から辿れない（フォーク網の SHA の恐れ）",
}


class GuardError(RuntimeError):
    """読めない（exit 2）。"""


def _yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:
        raise GuardError("pyyaml_missing") from exc
    return yaml


def load_yaml(text: str):
    """安全ローダーで1文書だけ読む。重複キー・アンカー・別名・明示タグは読めない扱い。"""
    yaml = _yaml()

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, bool, int)) or key in keys:
                raise ValueError("duplicate_or_complex_key")
            keys.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise ValueError("alias")
            if getattr(event, "anchor", None):
                raise ValueError("anchor")
            # 明示のタグ（!!str など）は読めない扱い。暗黙の解決だけを使う
            if getattr(event, "tag", None) is not None:
                raise ValueError("tag")
        documents = list(yaml.load_all(text, Loader=StrictLoader))
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError("unreadable") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("unreadable")
    return documents[0]


def _mentions_gate(uses: str) -> bool:
    lowered = uses.casefold()
    return "repo-preflight" in lowered or "preflight-gate" in lowered


def is_self_repository(name: str | None) -> bool:
    return (name or "").strip().casefold() == SELF_REPOSITORY.casefold()


def find_callers(data: dict, self_repo: bool = False) -> tuple[list[dict], list[dict]]:
    """jobs の中から preflight-gate を呼ぶ job を返す。(呼び出し, 赤の種類) の組。

    self_repo は検査する repo が repo-preflight 本体のときだけ True。そのときに限り、
    `uses` が SELF_TARGET と完全一致（@ なし）し、`preflight-ref` が SELF_PREFLIGHT_REF と
    完全一致する job を本体の自己検査として通す。それ以外の形は従来どおり赤。
    """
    jobs = data.get("jobs")
    callers: list[dict] = []
    findings: list[dict] = []
    if jobs is None:
        return callers, findings
    if not isinstance(jobs, dict):
        raise ValueError("unreadable")
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            raise ValueError("unreadable")
        uses = job.get("uses")
        if uses is None:
            continue
        if not isinstance(uses, str):
            raise ValueError("unreadable")
        if not _mentions_gate(uses):
            continue
        target, _, ref = uses.strip().partition("@")
        if self_repo and uses == SELF_TARGET:
            with_ = job.get("with")
            if (
                isinstance(with_, dict)
                and with_.get("preflight-ref") == SELF_PREFLIGHT_REF
            ):
                callers.append(
                    {
                        "job": str(job_id),
                        "uses_ref": "",
                        "preflight_ref": None,
                        "self": True,
                    }
                )
                continue
        if target.casefold() != EXPECTED_TARGET.casefold():
            findings.append({"kind": "foreign_caller", "job": str(job_id)})
            continue
        with_ = job.get("with")
        if with_ is not None and not isinstance(with_, dict):
            raise ValueError("unreadable")
        preflight_ref = (with_ or {}).get("preflight-ref")
        callers.append(
            {"job": str(job_id), "uses_ref": ref, "preflight_ref": preflight_ref}
        )
    return callers, findings


def check_caller(caller: dict) -> list[str]:
    if caller.get("self"):
        return []
    uses_ref = caller["uses_ref"]
    preflight_ref = caller["preflight_ref"]
    found: list[str] = []
    if not SHA_RE.match(uses_ref):
        found.append("uses_not_sha")
    if preflight_ref is None:
        found.append("preflight_ref_missing")
    elif not isinstance(preflight_ref, str) or not SHA_RE.match(preflight_ref):
        found.append("preflight_ref_not_sha")
    if not found and uses_ref != preflight_ref:
        found.append("mismatch")
    return found


def _git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), *args], capture_output=True
    )
    if result.returncode != 0:
        raise GuardError("git_failed")
    return result.stdout if binary else result.stdout.decode("utf-8", "strict")


def resolve(repo: Path, ref: str) -> str:
    try:
        commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GuardError as exc:
        raise GuardError("ref_unreadable") from exc
    commit = commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise GuardError("ref_unreadable")
    return commit


def read_workflows(repo: Path, commit: str) -> dict[str, str | None]:
    """workflow のパス → 中身（UTF-8 で読めなければ None）。"""
    kind = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", f"{commit}:.github/workflows"],
        capture_output=True,
        text=True,
    )
    if kind.returncode != 0 or kind.stdout.strip() != "tree":
        return {}
    listing = _git(repo, "ls-tree", "-z", "--name-only", f"{commit}:.github/workflows")
    files: dict[str, str | None] = {}
    for name in filter(None, listing.split("\0")):
        if name.casefold().endswith((".yml", ".yaml")):
            path = f".github/workflows/{name}"
            raw = _git(repo, "show", f"{commit}:{path}", binary=True)
            try:
                files[path] = raw.decode("utf-8")
            except UnicodeDecodeError:
                files[path] = None
    return files


def evaluate_definitions(
    files: dict[str, str | None], self_repo: bool = False
) -> tuple[int, list[dict]]:
    findings: list[dict] = []
    total = 0
    for path, text in sorted(files.items()):
        try:
            if text is None:
                raise ValueError("unreadable")
            callers, foreign = find_callers(load_yaml(text), self_repo)
        except ValueError:
            findings.append({"kind": "unreadable", "file": path, "job": ""})
            continue
        for item in foreign:
            findings.append({"kind": item["kind"], "file": path, "job": item["job"]})
        total += len(callers)
        for caller in callers:
            for kind in check_caller(caller):
                findings.append({"kind": kind, "file": path, "job": caller["job"]})
    return total, findings


def load_protected(path: Path = PROTECTED_PATHS) -> tuple[tuple[str, ...], frozenset]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        prefixes = tuple(p.casefold() for p in data["prefixes"])
        exact = frozenset(p.casefold() for p in data["exact"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GuardError("protected_paths_unreadable") from exc
    if not prefixes or not all(p.endswith("/") for p in prefixes):
        raise GuardError("protected_paths_unreadable")
    return prefixes, exact


def is_protected(path: str, prefixes, exact) -> bool:
    lowered = path.casefold().replace("\\", "/")
    return lowered in exact or lowered.startswith(prefixes)


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    merge_base = _git(repo, "merge-base", base, head).strip()
    out = _git(repo, "diff", "--name-only", "-z", "--no-renames", f"{merge_base}", head)
    return [p for p in out.split("\0") if p]


RELEASE_TAG_GLOB = "v[0-9]*"


def released_sha(tool: Path, sha: str) -> bool:
    """SHA が repo-preflight の保護した版のタグ（v*）から辿れるか（品質判定 M5）。

    tool は repo-preflight 本体から全履歴とタグを取った clone。フォーク網だけにある
    commit（imposter commit）はタグから辿れないので False になる。
    """
    if not SHA_RE.match(sha or ""):
        return False
    probe = subprocess.run(
        ["git", "-C", str(tool), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    if probe.returncode != 0:
        return False
    tags = subprocess.run(
        ["git", "-C", str(tool), "tag", "--list", RELEASE_TAG_GLOB, "--contains", sha],
        capture_output=True,
        text=True,
    )
    if tags.returncode != 0:
        raise GuardError("tool_unreadable")
    return bool(tags.stdout.strip())


def verification_policy_changes(
    base_workflows: dict, head_workflows: dict, self_repo: bool = False
) -> list[dict]:
    """必須検査・承認入口の意味を、通常のCI変更承認から分離する。"""
    findings = []
    for path, text in base_workflows.items():
        try:
            before = load_yaml(text)
            jobs = before.get("jobs", {})
            callers, _ = find_callers(before, self_repo)
            controls_guard = (self_repo and path == SELF_TARGET[2:]) or any(
                isinstance(job, dict)
                and (key == "base-guard" or job.get("name") == "base-guard")
                for key, job in jobs.items()
            )
            if not callers and not controls_guard:
                continue
            after = load_yaml(head_workflows.get(path))
            if not controls_guard:
                # 正式な版への固定更新だけは既存のSHA・release検査へ委ねる。
                before, after = copy.deepcopy(before), copy.deepcopy(after)
                for caller in callers:
                    for data in (before, after):
                        job = data["jobs"][caller["job"]]
                        job["uses"] = "registered-preflight-caller"
                        job.get("with", {}).pop("preflight-ref", None)
            changed = before != after
        except (ValueError, TypeError, AttributeError, KeyError):
            changed = True
        if changed:
            findings.append(
                {
                    "kind": "verification_policy_changed",
                    "file": path,
                    "job": "",
                    "side": "head",
                }
            )
    return findings


def assert_live_context(context: dict, base: str, head: str) -> None:
    """信頼済みworkflowが渡した対象をGitHubの現在値へ照合する。"""
    repository = context["repository"]
    number = context["pull_request"]
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or type(number) is not int
        or number <= 0
    ):
        raise GuardError("live_context_invalid")
    token = os.environ.get("REPO_PREFLIGHT_CONTEXT_TOKEN")
    if not token:
        raise GuardError("live_context_unavailable")
    request = Request(
        f"https://api.github.com/repos/{repository}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversize")
        data = json.loads(raw)
        matches = (
            data["state"] == "open"
            and data["number"] == number
            and data["base"]["repo"]["id"] == context["repository_id"]
            and data["base"]["repo"]["full_name"].casefold() == repository.casefold()
            and "refs/heads/" + data["base"]["ref"] == context["base_ref"]
            and data["base"]["sha"] == base
            and data["head"]["sha"] == head
        )
        if not matches:
            raise ValueError("changed")
    except Exception:
        raise GuardError("live_context_unavailable_or_changed") from None


def decision_request(repo: Path, base: str, head: str, context: dict) -> dict:
    """確認する対象を読み出すだけで、承認や署名を作らない。"""
    from protected_change_decisions import BUNDLE_PATH, TRUST_PATH, protected_delta

    prefixes, exact = load_protected()
    paths = sorted(
        path
        for path in changed_paths(repo, base, head)
        if is_protected(path, prefixes, exact) and path != BUNDLE_PATH
    )
    policy_hash = None
    try:
        size = int(_git(repo, "cat-file", "-s", f"{base}:{TRUST_PATH}"))
        if not 0 < size <= 256 * 1024:
            raise GuardError("trust_size_limit")
        policy_hash = hashlib.sha256(
            _git(repo, "show", f"{base}:{TRUST_PATH}", binary=True)
        ).hexdigest()
    except GuardError:
        pass
    return {
        "schema": "repo-preflight.protected-change-request/v1",
        "status": "unsigned_request_not_authorization",
        "human_verified": False,
        "base_sha": base,
        "head_sha": head,
        "scope": {
            **context,
            "operation": "protected-change-check",
            "paths": paths,
            "protected_delta_sha256": (
                protected_delta(repo, base, head, paths) if paths else None
            ),
            "policy_sha256": policy_hash,
        },
        "trust_status": "available_for_verification" if policy_hash else "unavailable",
        "requires_trust_administration": TRUST_PATH in paths,
    }


def evaluate(
    repo: Path,
    base_ref: str,
    head_ref: str | None = None,
    allow_absent: bool = False,
    trusted_tool: Path | None = None,
    self_repository: str | None = None,
    decision_context: dict | None = None,
) -> dict:
    self_repo = is_self_repository(self_repository)
    base = resolve(repo, base_ref)
    base_workflows = read_workflows(repo, base)
    base_total, findings = evaluate_definitions(base_workflows, self_repo)
    decision = {"status": "not_required", "reason": "no_protected_change"}
    for item in findings:
        item["side"] = "base"
    unreadable = any(f["kind"] == "unreadable" for f in findings)
    if base_total == 0 and not unreadable and not allow_absent:
        findings.append({"kind": "no_caller", "file": "", "job": "", "side": "base"})
    if head_ref is not None:
        head = resolve(repo, head_ref)
        head_workflows = read_workflows(repo, head)
        head_total, head_findings = evaluate_definitions(head_workflows, self_repo)
        for item in head_findings:
            item["side"] = "head"
        findings += head_findings
        if trusted_tool is not None:
            for path, text in sorted(read_workflows(repo, head).items()):
                try:
                    callers, _ = find_callers(load_yaml(text)) if text else ([], [])
                except ValueError:
                    continue  # unreadable として上で数えている
                for caller in callers:
                    if SHA_RE.match(caller["uses_ref"]) and not released_sha(
                        trusted_tool, caller["uses_ref"]
                    ):
                        findings.append(
                            {
                                "kind": "preflight_ref_unreleased",
                                "file": path,
                                "job": caller["job"],
                                "side": "head",
                            }
                        )
        if head_total == 0 and (base_total > 0 or not allow_absent):
            if not any(f["kind"] == "unreadable" for f in head_findings):
                findings.append(
                    {"kind": "caller_removed", "file": "", "job": "", "side": "head"}
                )
        prefixes, exact = load_protected()
        protected = [
            path
            for path in changed_paths(repo, base, head)
            if is_protected(path, prefixes, exact)
        ]
        if protected:
            findings += verification_policy_changes(
                base_workflows, head_workflows, self_repo
            )
            if decision_context is None:
                decision = {"status": "blocked", "reason": "decision_context_required"}
            elif findings:
                decision = {
                    "status": "blocked",
                    "reason": "non_overridable_invariant_failed",
                }
            else:
                from protected_change_decisions import verify_decisions

                decision = verify_decisions(
                    repo=repo,
                    base=base,
                    head=head,
                    context=decision_context,
                    protected_paths=protected,
                )
            if decision.get("status") != "approved":
                for path in protected:
                    findings.append(
                        {
                            "kind": "protected_path_changed",
                            "file": path,
                            "job": "",
                            "side": "head",
                        }
                    )
    if findings:
        status = "blocked"
    elif base_total == 0:
        status = "absent"
    else:
        status = "pass"
    return {
        "schema": SCHEMA,
        "status": status,
        "callers": base_total,
        "findings": findings,
        "protected_change_decision": decision,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="検査する repository")
    parser.add_argument(
        "--base-ref", required=True, help="比較元（保護された側）の ref か SHA"
    )
    parser.add_argument(
        "--head-ref",
        help="PR 側の ref か SHA（pull_request_target の別ワークフローから渡す）",
    )
    parser.add_argument(
        "--allow-absent",
        action="store_true",
        help="比較元に呼び出しが一つも無いとき（導入前）は止めない。読めない定義は見送らない",
    )
    parser.add_argument(
        "--trusted-tool",
        help="repo-preflight 本体の clone（全履歴とタグ）。PR 側の SHA が版のタグから辿れるかを見る",
    )
    parser.add_argument(
        "--self-repository",
        help="検査する repo の owner/名前（CI の github.repository）。repo-preflight 本体のときだけ自己検査の呼び出しを通す",
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出す")
    parser.add_argument("--repository", help="信頼済みcallerが渡す対象owner/name")
    parser.add_argument("--repository-id", type=int, help="GitHub repository ID")
    parser.add_argument("--pull-request", type=int, help="対象PR番号")
    parser.add_argument("--base-branch", help="対象base branchの完全ref")
    parser.add_argument(
        "--verify-github-context",
        action="store_true",
        help="判定の前後にGitHubの現在の対象を照合する",
    )
    parser.add_argument(
        "--decision-request",
        action="store_true",
        help="署名・承認をせず、確認対象の差分digestをJSONで表示する",
    )
    args = parser.parse_args(argv)
    fields = (args.repository, args.repository_id, args.pull_request, args.base_branch)
    if any(value is not None for value in fields) and not all(
        value is not None for value in fields
    ):
        parser.error(
            "判断の対象にはrepository・repository-id・pull-request・base-branchの全指定が必要です"
        )
    context = (
        dict(zip(("repository", "repository_id", "pull_request", "base_ref"), fields))
        if all(value is not None for value in fields)
        else None
    )
    if (args.verify_github_context or args.decision_request) and (
        context is None or args.head_ref is None
    ):
        parser.error("現在値照合には判断の対象とhead-refが必要です")
    try:
        if args.decision_request:
            base_sha = resolve(Path(args.repo), args.base_ref)
            head_sha = resolve(Path(args.repo), args.head_ref)
            if args.verify_github_context:
                assert_live_context(context, base_sha, head_sha)
            request = decision_request(Path(args.repo), base_sha, head_sha, context)
            if args.verify_github_context:
                assert_live_context(context, base_sha, head_sha)
            print(json.dumps(request, ensure_ascii=False, indent=2))
            return 0
        if args.verify_github_context:
            base_sha = resolve(Path(args.repo), args.base_ref)
            head_sha = resolve(Path(args.repo), args.head_ref)
            assert_live_context(context, base_sha, head_sha)
        report = evaluate(
            Path(args.repo),
            base_sha if args.verify_github_context else args.base_ref,
            head_sha if args.verify_github_context else args.head_ref,
            args.allow_absent,
            Path(args.trusted_tool) if args.trusted_tool else None,
            args.self_repository,
            context,
        )
        if args.verify_github_context:
            assert_live_context(context, base_sha, head_sha)
    except GuardError as exc:
        reason = (
            "PyYAML が無いので止めます"
            if str(exc) == "pyyaml_missing"
            else "比較元か PR 側の定義を読めないので止めます"
        )
        print(reason, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["findings"]:
            side = "比較元" if item["side"] == "base" else "PR 側"
            where = f"{item['file']} {item['job']}".strip()
            print(
                f"失敗: {side}: {KINDS[item['kind']]}"
                + (f"（{where}）" if where else ""),
                file=sys.stderr,
            )
        if report["protected_change_decision"]["status"] == "blocked":
            print(
                "判断証拠: " + report["protected_change_decision"]["reason"],
                file=sys.stderr,
            )
        if report["status"] == "pass":
            print(
                f"base-guard: 比較元の呼び出し {report['callers']} 件はそろっています"
            )
        elif report["status"] == "absent":
            print("base-guard: 比較元に呼び出しが無い（導入前）ので見送ります")
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())

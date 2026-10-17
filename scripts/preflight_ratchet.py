#!/usr/bin/env python3
"""repo-preflight の検査結果をラチェットで判定する（CI ゲート用）。

readiness_scan.py を検査モードで実行し、自動検査ごとの指摘件数を基準値ファイルと比べる。

- 件数が基準値より増えた検査が一つでもあれば `blocked`（exit 1）
- 基準値より減ったのに基準値ファイルを締めていなければ `stale_baseline`（exit 1）。
  `--update-baseline` で締める。基準値を上げる更新はこのスクリプトでは行わない。
- `--baseline-base-ref` を渡すと、比較元の基準値より緩めた基準値も `blocked` にする。
- 検査そのものが失敗した（tool_error）ときは基準値に関係なく exit 2。

件数だけを扱い、指摘の中身（path・secret）は出力しない。
基準値で見逃せるのは CI の判定だけで、公開の判定（publish_gate.py）は基準値を使わない。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCHEMA = "repo-preflight.ratchet/v1"
BASELINE_SCHEMA = "repo-preflight.ratchet-baseline/v1"
DEFAULT_BASELINE = ".repo-preflight/ratchet-baseline.json"
SCAN_SCRIPT = Path(__file__).resolve().parent / "readiness_scan.py"

# readiness_scan.py が status の判定に使う自動検査（base_ref なしの全体検査）。
# 指摘件数を持つ検査はその件数、持たない検査は fail / unknown を 1 件と数える。
RATCHET_CHECKS = (
    "clean_worktree",
    "required_documents",
    "secret_scan",
    "personal_path_scan",
    "commit_identity",
    "dependency_configuration",
    "ci_configuration",
    "origin",
    "repository_consistency",
)


class RatchetError(Exception):
    """基準値や検査結果を読めない。判定に使わず exit 2 にする。"""


def count_findings(name: str, check: dict) -> int:
    status = check.get("status")
    if status == "tool_error":
        raise RatchetError(f"{name}: tool_error")
    if name == "secret_scan":
        count = check.get("finding_count", 0)
    elif name == "personal_path_scan":
        count = len(check.get("files", [])) + len(check.get("unscanned_files", []))
    elif name == "required_documents":
        count = len(check.get("missing", [])) + len(check.get("invalid", []))
    elif name == "repository_consistency":
        count = check.get("finding_count", 0)
    elif name == "commit_identity":
        count = check.get("mismatch_count", 0)
    else:
        count = 0
    if not isinstance(count, int) or count < 0:
        raise RatchetError(f"{name}: invalid count")
    # 件数欄が 0 でも fail / unknown なら 1 件として数え、通さない側に倒す
    if status in {"fail", "unknown"} and count == 0:
        count = 1
    return count


def counts_from_report(report: dict) -> dict[str, int]:
    if report.get("status") == "tool_error":
        raise RatchetError("scan: tool_error")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise RatchetError("scan: checks missing")
    counts = {}
    for name in RATCHET_CHECKS:
        check = checks.get(name)
        if not isinstance(check, dict):
            raise RatchetError(f"{name}: missing")
        counts[name] = count_findings(name, check)
    return counts


def parse_baseline(text: str, label: str) -> dict[str, int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"{label}: invalid JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != BASELINE_SCHEMA:
        raise RatchetError(f"{label}: schema must be {BASELINE_SCHEMA}")
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise RatchetError(f"{label}: counts missing")
    unknown = sorted(set(counts) - set(RATCHET_CHECKS))
    if unknown:
        raise RatchetError(f"{label}: unknown checks {unknown}")
    result = {}
    for name in RATCHET_CHECKS:
        value = counts.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RatchetError(f"{label}: {name} must be a non-negative integer")
        result[name] = value
    return result


def render_baseline(counts: dict[str, int]) -> str:
    data = {
        "schema": BASELINE_SCHEMA,
        "counts": {name: counts[name] for name in RATCHET_CHECKS},
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def read_baseline_at_ref(repo: Path, ref: str, rel_path: str) -> dict[str, int] | None:
    """比較元 ref の基準値。ファイルが無ければ None（初回導入）。"""
    verify = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        raise RatchetError("baseline base ref: not found")
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{rel_path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if shown.returncode != 0:
        exists = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{rel_path}"],
            capture_output=True,
        )
        if exists.returncode != 0:
            return None
        raise RatchetError("baseline base ref: unreadable")
    return parse_baseline(shown.stdout, "baseline at base ref")


def run_scan(repo: Path, scan_args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_SCRIPT), "--repo", str(repo), *scan_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RatchetError("scan: output is not JSON") from exc


def evaluate(
    current: dict[str, int],
    baseline: dict[str, int] | None,
    base_baseline: dict[str, int] | None,
) -> dict:
    """基準値ファイルが無い repo は全検査 0 件を基準にする。"""
    increased = []
    improved = []
    loosened = []
    effective = baseline or {name: 0 for name in RATCHET_CHECKS}
    for name in RATCHET_CHECKS:
        if current[name] > effective[name]:
            increased.append(
                {"check": name, "baseline": effective[name], "current": current[name]}
            )
        elif current[name] < effective[name]:
            improved.append(
                {"check": name, "baseline": effective[name], "current": current[name]}
            )
    # 比較元に基準値が無い = 初回導入。導入時の値は人のレビューで決める
    if baseline is not None and base_baseline is not None:
        for name in RATCHET_CHECKS:
            if baseline[name] > base_baseline[name]:
                loosened.append(
                    {
                        "check": name,
                        "base": base_baseline[name],
                        "baseline": baseline[name],
                    }
                )
    if increased or loosened:
        status = "blocked"
    elif improved:
        status = "stale_baseline"
    else:
        status = "pass"
    return {
        "schema": SCHEMA,
        "status": status,
        "counts": current,
        "baseline": baseline,
        "increased": increased,
        "improved": improved,
        "loosened": loosened,
    }


def tighten(current: dict[str, int], baseline: dict[str, int] | None) -> dict:
    """基準値を締める。増えた検査があれば書かない。"""
    if baseline is None:
        return {"written": True, "counts": dict(current)}
    if any(current[name] > baseline[name] for name in RATCHET_CHECKS):
        return {"written": False, "counts": baseline}
    return {
        "written": True,
        "counts": {name: min(current[name], baseline[name]) for name in RATCHET_CHECKS},
    }


MESSAGES = {
    "pass": "基準値から増えていません。",
    "blocked": (
        "基準値より指摘が増えた、または基準値が緩められました。"
        "基準値ファイルが無い repo は全検査 0 件が基準です。初回導入は "
        "`--update-baseline` で作り、人がレビューしてから commit してください。"
    ),
    "stale_baseline": (
        "指摘が減りました。同じ変更で `python scripts/preflight_ratchet.py "
        "--repo . --update-baseline` を実行して基準値を締めてください。"
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE,
        help=f"repo からの相対 path（既定: {DEFAULT_BASELINE}）",
    )
    parser.add_argument(
        "--baseline-base-ref",
        help="この ref の基準値より緩めていないかも確かめる（PR の比較元など）",
    )
    parser.add_argument(
        "--consistency-base-ref",
        help="readiness_scan.py へそのまま渡す",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="基準値を作る、または締める（上げる更新はしない）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    rel = Path(args.baseline)
    if rel.is_absolute() or ".." in rel.parts:
        print(json.dumps({"schema": SCHEMA, "status": "tool_error"}))
        print("error: --baseline must be a path inside the repository", file=sys.stderr)
        return 2
    baseline_path = repo / rel
    scan_args = []
    if args.consistency_base_ref:
        scan_args += ["--consistency-base-ref", args.consistency_base_ref]
    try:
        baseline = (
            parse_baseline(baseline_path.read_text(encoding="utf-8"), "baseline")
            if baseline_path.is_file()
            else None
        )
        base_baseline = (
            read_baseline_at_ref(repo, args.baseline_base_ref, rel.as_posix())
            if args.baseline_base_ref
            else None
        )
        current = counts_from_report(run_scan(repo, scan_args))
    except RatchetError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "tool_error", "issue": str(exc)}))
        return 2

    if args.update_baseline:
        result = tighten(current, baseline)
        if not result["written"]:
            report = evaluate(current, baseline, None)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                "error: 指摘が増えた検査があるため基準値を更新しません。",
                file=sys.stderr,
            )
            return 1
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(render_baseline(result["counts"]), encoding="utf-8")
        baseline = result["counts"]

    report = evaluate(current, baseline, base_baseline)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(MESSAGES[report["status"]], file=sys.stderr)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

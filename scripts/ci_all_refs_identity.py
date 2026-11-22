"""公開refの定期監査用の名義検査。"""

import argparse
import subprocess

import identity_gate as gate

REVIEWED_COMMIT = "93e8425972003617a75bfb19991c0512a205e919"
REVIEWED_FILES = {
    "tests/test_readiness_scan.py": (
        "e12858121278dd227ff3dd551f64fed74be755a665b721e7ff3e975e861a51c5",
        {
            1300: "個人メールの形",
            3204: "名簿にないメールの形",
            3220: "個人メールの形",
        },
    ),
    "tests/test_tool_traces_allowed.py": (
        "73958d3c2799e927a15676c1f538bf44c725afedd1660cb1f0897f896d6f4fca",
        {
            29: "名簿にないメールの形",
            36: "個人メールの形",
            53: "名簿にないメールの形",
        },
    ),
}


def reviewed_finding_kinds(policy, repo):
    expected = {}
    for path, (digest, lines) in REVIEWED_FILES.items():
        if gate._blob_sha256(repo, f"{REVIEWED_COMMIT}:{path}") != digest:
            return set()
        expected.update({(path, line): kind for line, kind in lines.items()})

    diff = gate._commit_diff(repo, REVIEWED_COMMIT, gate.git)
    skip = gate._exception_skipper(
        repo, REVIEWED_COMMIT, gate.load_fixture_exceptions()
    )
    added = gate._added_from_diff(diff, skip)
    rows = list(gate._diff_rows(diff, skip))
    if "\n".join(row for _, _, row in rows) != added:
        return set()

    proven = set()
    other_rows = []
    for path, line, row in rows:
        key = (path, line)
        if key not in expected:
            other_rows.append(row)
            continue
        if key in proven or gate.diff_findings(policy, row, "") != [expected[key]]:
            return set()
        proven.add(key)

    kinds = set(expected.values())
    if (
        proven != expected.keys()
        or set(gate.diff_findings(policy, added, "")) != kinds
        or gate.diff_findings(policy, "\n".join(other_rows), "")
    ):
        return set()
    return kinds


def scan_range(policy, repo, revision):
    issues = gate.check_public_range(policy, repo, revision, diffs=True)
    prefix = f"{REVIEWED_COMMIT[:8]}: 差分に"
    if any(
        issue.startswith(prefix) for issue in issues
    ) and REVIEWED_COMMIT in gate.commit_ids(repo, revision, gate.git):
        reviewed = [
            prefix + kind for kind in sorted(reviewed_finding_kinds(policy, repo))
        ]
        if all(issue in issues for issue in reviewed):
            for issue in reviewed:
                issues.remove(issue)
    return issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--allowed-identities", required=True)
    parser.add_argument("--range", dest="revision", required=True)
    args = parser.parse_args()
    try:
        policy = gate.load_public_policy(args.allowed_identities, args.repo)
        return gate._report(scan_range(policy, args.repo, args.revision))
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        print('{"status":"tool_error","reason":"identity_input_unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

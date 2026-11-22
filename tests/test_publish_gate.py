"""publish_gate の判定。git / gh / 検査本体は偽の runner に置き換え、外部へは何も送らない。"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_gate as p

SLUG = "example-owner/example-repo"
HEAD = "a" * 40
REPO = Path("/nonexistent/repo")
GATE_WORKFLOW = """name: preflight
on: pull_request
jobs:
  gate:
    uses: example-owner/repo-preflight/.github/workflows/preflight-gate.yml@0123abc # v0.7.0
"""


@pytest.fixture(autouse=True)
def public_name_policy(tmp_path, monkeypatch):
    policy = tmp_path / "identity-policy.txt"
    policy.write_text("synthetic-private-marker\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)


def test_missing_git_reports_tool_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert p.main(["--repo", str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert (report["status"], report["issue"], report["applied"]) == (
        "tool_error",
        "git_launch_failed",
        False,
    )


class FakeRunner:
    def __init__(
        self,
        *,
        origin=f"https://github.com/{SLUG}.git",
        visibility="PRIVATE",
        archived=False,
        refs=None,
        missing=(),
        head=HEAD,
        scan_status="pass",
        edit_ok=True,
        visibility_after="PUBLIC",
        workflows=None,
        object_types=None,
    ):
        self.workflows = (
            workflows
            if workflows is not None
            else {".github/workflows/preflight.yml": GATE_WORKFLOW}
        )
        self.origin = origin
        self.visibility = visibility
        self.archived = archived
        self.refs = refs if refs is not None else [(HEAD, "refs/heads/main")]
        self.missing = set(missing)
        self.object_types = object_types or {}
        self.head = head
        self.scan_status = scan_status
        self.edit_ok = edit_ok
        self.visibility_after = visibility_after
        self.calls = []
        self.edited = False

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == "git":
            return self.git(cmd[cmd.index("-C") + 2 :])
        if cmd[:3] == ["gh", "repo", "view"]:
            vis = self.visibility_after if self.edited else self.visibility
            assert cmd[3] == f"github.com/{SLUG}"
            data = {
                "id": "R_example",
                "nameWithOwner": SLUG,
                "visibility": vis,
                "isArchived": self.archived,
                "defaultBranchRef": {"name": "main"},
            }
            return self.done(0, json.dumps(data))
        if cmd[:3] == ["gh", "repo", "edit"]:
            self.edited = self.edit_ok
            return self.done(0 if self.edit_ok else 1)
        if cmd[1].endswith("readiness_scan.py"):
            checks = {"secret_scan": {"status": "pass"}}
            assert "--require-private-identity" in cmd
            roots = dict.fromkeys([*(s for s, _ in self.refs), self.head])
            checks["private_identity_scan"] = {
                "status": "pass",
                "closure_revisions_sha256": hashlib.sha256(
                    "\n".join(roots).encode()
                ).hexdigest(),
            }
            if self.scan_status != "pass":
                checks["personal_path_scan"] = {"status": "fail"}
            return self.done(
                0,
                json.dumps(
                    {"status": self.scan_status, "head": self.head, "checks": checks}
                ),
            )
        raise AssertionError(cmd)

    def git(self, args):
        if args[:2] == ["remote", "get-url"]:
            return self.done(0, self.origin + "\n")
        if args[0] == "ls-remote":
            return self.done(0, "".join(f"{s}\t{r}\n" for s, r in self.refs))
        if args[0] == "rev-list" or args[:2] == ["rev-parse", "--revs-only"]:
            return self.done(
                0, "".join(f"{s}\n" for s, _ in self.refs if s not in self.missing)
            )
        if args[0] == "ls-tree":
            return self.done(0, "".join(f"{path}\n" for path in self.workflows))
        if args[0] == "show":
            return self.done(0, self.workflows[args[1].split(":", 1)[1]])
        if args[0] == "cat-file":
            if args[-1] in self.missing:
                return self.done(1)
            return self.done(0, self.object_types.get(args[-1], "blob") + "\n")
        if args[0] == "rev-parse":
            return self.done(0, self.head + "\n")
        raise AssertionError(args)

    @staticmethod
    def done(code, out=""):
        return subprocess.CompletedProcess([], code, out, "")

    def edit_calls(self):
        return [c for c in self.calls if c[:3] == ["gh", "repo", "edit"]]


def run(runner, **kwargs):
    kwargs.setdefault("apply", False)
    kwargs.setdefault("confirm", None)
    if kwargs["apply"] and "reviewed_snapshot" not in kwargs:
        preview = p.evaluate(REPO, apply=False, confirm=None, run=FakeRunner())
        kwargs["reviewed_snapshot"] = preview["reviewed_snapshot"]
    return p.evaluate(REPO, run=runner, **kwargs)


@pytest.mark.parametrize("prefix", ["refs/heads/", "refs/tags/"])
def test_private_ref_name_blocks_and_is_redacted(prefix):
    marker = "synthetic-private-marker"
    runner = FakeRunner(refs=[(HEAD, "refs/heads/main"), (HEAD, prefix + marker)])
    result = run(runner)
    assert result["issue"] == "private_identity_in_public_name"
    assert marker not in json.dumps(result)
    assert not runner.edit_calls()


def test_github_repository_name_must_match_origin():
    class ChangedName(FakeRunner):
        def __call__(self, cmd):
            proc = super().__call__(cmd)
            if cmd[:3] == ["gh", "repo", "view"]:
                state = json.loads(proc.stdout)
                state["nameWithOwner"] = "different/repository"
                return self.done(0, json.dumps(state))
            return proc

    runner = ChangedName()
    result = run(runner)
    assert result["issue"] == "github_repository_name_differs_from_origin"
    assert not result["edit_attempted"]
    assert "different/repository" not in json.dumps(result)


def test_private_repository_name_blocks_before_reporting_it():
    runner = FakeRunner(origin="https://github.com/synthetic-private-marker/repo")
    result = run(runner)
    assert result["issue"] == "private_identity_in_public_name"
    assert "repository" not in result
    assert not runner.edit_calls()


def test_unreadable_ref_name_stops_without_printing_it():
    runner = FakeRunner(refs=[(HEAD, "refs/heads/main"), (HEAD, "refs/tags/\udcff")])
    result = run(runner)
    assert result["issue"] == "public_name_encoding_unverified"
    assert not runner.edit_calls()


def test_既定はdry_runで何も変えない():
    runner = FakeRunner()
    result = run(runner)
    assert result["status"] == "ready"
    assert runner.edit_calls() == []


def test_全部通って確認名が一致したときだけ公開する():
    runner = FakeRunner()
    result = run(runner, apply=True, confirm=SLUG)
    assert result["status"] == "published"
    assert result["visibility_after"] == "PUBLIC"
    assert runner.edit_calls() == [
        [
            "gh",
            "repo",
            "edit",
            f"github.com/{SLUG}",
            "--visibility",
            "public",
            "--accept-visibility-change-consequences",
        ]
    ]


def test_確認名が違えば公開しない():
    runner = FakeRunner()
    result = run(runner, apply=True, confirm="example-owner/other")
    assert (result["status"], result["issue"]) == ("blocked", "confirmation_mismatch")
    assert runner.edit_calls() == []


def test_検査がpassでなければ公開しない():
    runner = FakeRunner(scan_status="blocked")
    result = run(runner, apply=True, confirm=SLUG)
    assert result["status"] == "blocked"
    assert result["failed_checks"] == ["personal_path_scan"]
    assert runner.edit_calls() == []


def test_検査のtool_errorも公開しない():
    runner = FakeRunner(scan_status="tool_error")
    result = run(runner, apply=True, confirm=SLUG)
    assert result["status"] == "tool_error"
    assert runner.edit_calls() == []


def test_未取得のremote_refがあれば公開しない():
    other = "b" * 40
    runner = FakeRunner(
        refs=[(HEAD, "refs/heads/main"), (other, "refs/heads/old")],
        missing=[other],
    )
    result = run(runner, apply=True, confirm=SLUG)
    assert result["issue"] == "remote_refs_not_fetched"
    assert runner.edit_calls() == []


def test_apply_requires_the_reviewed_snapshot():
    runner = FakeRunner()
    for reviewed in (None, "stale"):
        result = run(runner, apply=True, confirm=SLUG, reviewed_snapshot=reviewed)
        assert result["issue"] == "reviewed_snapshot_mismatch"
    assert not runner.edit_calls()


def test_review_of_older_refs_cannot_authorize_new_tag():
    runner = FakeRunner()
    reviewed = run(runner)["reviewed_snapshot"]
    runner.refs.append((HEAD, "refs/tags/new"))
    result = run(runner, apply=True, confirm=SLUG, reviewed_snapshot=reviewed)
    assert result["issue"] == "reviewed_snapshot_mismatch"
    assert not runner.edit_calls()


def test_scan_of_different_history_blocks_publication():
    runner = FakeRunner()

    def scan_other_roots(cmd):
        result = runner(cmd)
        if len(cmd) > 1 and cmd[1].endswith("readiness_scan.py"):
            report = json.loads(result.stdout)
            report["checks"]["private_identity_scan"][
                "closure_revisions_sha256"
            ] = "stale"
            return runner.done(0, json.dumps(report))
        return result

    result = run(scan_other_roots, apply=True, confirm=SLUG)
    assert result["issue"] == "scan_history_differs_from_review"
    assert not runner.edit_calls()


def test_tag_added_after_scan_blocks_publication():
    runner = FakeRunner()

    def add_tag_after_scan(cmd):
        result = runner(cmd)
        if len(cmd) > 1 and cmd[1].endswith("readiness_scan.py"):
            runner.refs.append((HEAD, "refs/tags/new"))
        return result

    result = run(add_tag_after_scan, apply=True, confirm=SLUG)
    assert result["issue"] == "origin_changed_during_gate"
    assert not runner.edit_calls()


def test_HEADがdefault_branchと違えば公開しない():
    runner = FakeRunner(head="c" * 40)
    result = run(runner, apply=True, confirm=SLUG)
    assert result["issue"] == "head_differs_from_origin_default_branch"
    assert runner.edit_calls() == []


def test_すでに公開なら何もしない():
    runner = FakeRunner(visibility="PUBLIC")
    result = run(runner, apply=True, confirm=SLUG)
    assert result["status"] == "already_public"
    assert runner.edit_calls() == []


def test_archiveされたrepoは公開しない():
    runner = FakeRunner(archived=True)
    assert run(runner, apply=True, confirm=SLUG)["issue"] == "repository_archived"


def test_GitHub以外のoriginは止める():
    runner = FakeRunner(origin="https://example.invalid/x/y.git")
    assert run(runner)["issue"] == "origin_not_github"


def test_指定したrepoとoriginが違えば止める():
    runner = FakeRunner()
    result = run(runner, expected_slug="example-owner/other")
    assert result["issue"] == "origin_differs_from_expected_repository"


def test_切り替え後にPUBLICを確認できなければtool_error():
    runner = FakeRunner(visibility_after="PRIVATE")
    result = run(runner, apply=True, confirm=SLUG)
    assert (result["status"], result["applied"]) == ("tool_error", True)


def test_apply単独は受け付けない(capsys):
    assert p.main(["--repo", str(REPO), "--apply"], run=FakeRunner()) == 2


def test_ssh形式のoriginも読む():
    runner = FakeRunner(origin=f"git@github.com:{SLUG}.git")
    assert run(runner)["repository"] == SLUG


def test_CIゲートが無ければ公開しない():
    runner = FakeRunner(workflows={".github/workflows/ci.yml": "name: ci\n"})
    result = run(runner, apply=True, confirm=SLUG)
    assert result["status"] == "blocked"
    assert result["issue"] == "ci_gate_missing"
    assert not runner.edited


def test_ラチェットの基準値が無ければ公開しない():
    runner = FakeRunner(missing={f"{HEAD}:.repo-preflight/ratchet-baseline.json"})
    result = run(runner, apply=True, confirm=SLUG)
    assert result["status"] == "blocked"
    assert result["issue"] == "ratchet_baseline_missing"
    assert not runner.edited


def test_基準値の置き場所を変えたゲートも認める():
    workflow = GATE_WORKFLOW + "    with:\n      baseline: ci/baseline.json\n"
    run = FakeRunner(
        workflows={".github/workflows/gate.yaml": workflow},
        missing={f"{HEAD}:.repo-preflight/ratchet-baseline.json"},
    )
    result = p.evaluate(REPO, apply=False, confirm=None, run=run)
    assert result["status"] == "ready"
    assert result["ci_gate"] == ".github/workflows/gate.yaml"


def test_コメント中のゲート名だけではゲートとみなさない():
    run = FakeRunner(
        workflows={".github/workflows/ci.yml": "# preflight-gate.yml を後で入れる\n"}
    )
    result = p.evaluate(REPO, apply=False, confirm=None, run=run)
    assert result["issue"] == "ci_gate_missing"


def test_shell_text_does_not_count_as_a_reusable_workflow():
    workflow = """name: example
on: pull_request
jobs:
  demo:
    runs-on: ubuntu-latest
    steps:
      - run: |
          cat <<'END'
          uses: example-owner/repo-preflight/.github/workflows/preflight-gate.yml@0123abc
          END
"""
    runner = FakeRunner(workflows={".github/workflows/ci.yml": workflow})
    result = run(runner, apply=True, confirm=SLUG)
    assert (result["status"], result["issue"]) == ("blocked", "ci_gate_missing")
    assert not runner.edit_calls()


def test_gate_checks_the_baseline_of_the_same_job():
    workflow = """name: preflight
on: pull_request
jobs:
  unrelated:
    uses: example-owner/repo-preflight/.github/workflows/example.yml@0123abc
    with:
      baseline: ci/other.json
""" + GATE_WORKFLOW.split("jobs:\n", 1)[1]
    runner = FakeRunner(
        workflows={".github/workflows/ci.yml": workflow},
        missing={f"{HEAD}:.repo-preflight/ratchet-baseline.json"},
    )
    result = run(runner, apply=True, confirm=SLUG)
    assert (result["status"], result["issue"]) == (
        "blocked",
        "ratchet_baseline_missing",
    )
    assert not runner.edit_calls()


def test_workflow_name_is_read_as_a_yaml_string():
    workflow = GATE_WORKFLOW.replace("name: preflight", "name: 2026-02-30")
    runner = FakeRunner(workflows={".github/workflows/ci.yml": workflow})
    assert run(runner)["status"] == "ready"
    assert not runner.edit_calls()


def test_baseline_directory_does_not_count_as_a_file():
    runner = FakeRunner(object_types={f"{HEAD}:{p.DEFAULT_BASELINE}": "tree"})
    result = run(runner, apply=True, confirm=SLUG)
    assert (result["status"], result["issue"]) == (
        "blocked",
        "ratchet_baseline_invalid",
    )
    assert not runner.edit_calls()


def test_local_reusable_workflow_must_exist():
    path = ".github/workflows/preflight-gate.yml"
    workflow = GATE_WORKFLOW.replace(
        f"example-owner/repo-preflight/{path}@0123abc", f"./{path}"
    )
    runner = FakeRunner(
        workflows={".github/workflows/ci.yml": workflow}, missing={f"{HEAD}:{path}"}
    )
    result = run(runner, apply=True, confirm=SLUG)
    assert (result["status"], result["issue"]) == ("blocked", "ci_gate_missing")
    assert not runner.edit_calls()


@pytest.mark.parametrize(
    "trigger,expected", [("workflow_call", "ready"), ("push", "blocked")]
)
def test_local_workflow_must_support_reusable_calls(trigger, expected):
    path = ".github/workflows/preflight-gate.yml"
    caller = GATE_WORKFLOW.replace(
        f"example-owner/repo-preflight/{path}@0123abc", f"./{path}"
    )
    runner = FakeRunner(
        workflows={
            ".github/workflows/ci.yml": caller,
            path: f"name: gate\non: {trigger}\n",
        }
    )
    result = run(runner)
    assert result["status"] == expected
    assert not runner.edit_calls()


def test_workflow_without_trigger_does_not_count_as_gate():
    caller = GATE_WORKFLOW.replace("on: pull_request\n", "")
    runner = FakeRunner(workflows={".github/workflows/ci.yml": caller})
    assert run(runner)["issue"] == "ci_gate_missing"
    assert not runner.edit_calls()

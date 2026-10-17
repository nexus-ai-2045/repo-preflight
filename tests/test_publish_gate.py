"""publish_gate の判定。git / gh / 検査本体は偽の runner に置き換え、外部へは何も送らない。"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_gate as p

SLUG = "example-owner/example-repo"
HEAD = "a" * 40
REPO = Path("/nonexistent/repo")
GATE_WORKFLOW = """name: preflight
jobs:
  gate:
    uses: example-owner/repo-preflight/.github/workflows/preflight-gate.yml@0123abc # v0.7.0
"""


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
            data = {
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
            if self.scan_status != "pass":
                checks["personal_path_scan"] = {"status": "fail"}
            return self.done(
                0, json.dumps({"status": self.scan_status, "checks": checks})
            )
        raise AssertionError(cmd)

    def git(self, args):
        if args[:2] == ["remote", "get-url"]:
            return self.done(0, self.origin + "\n")
        if args[0] == "ls-remote":
            return self.done(0, "".join(f"{s}\t{r}\n" for s, r in self.refs))
        if args[0] == "ls-tree":
            return self.done(0, "".join(f"{path}\n" for path in self.workflows))
        if args[0] == "show":
            return self.done(0, self.workflows[args[1].split(":", 1)[1]])
        if args[0] == "cat-file":
            return self.done(1 if args[-1] in self.missing else 0)
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
    return p.evaluate(REPO, run=runner, **kwargs)


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
            SLUG,
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
    run = FakeRunner(workflows={".github/workflows/ci.yml": "name: ci\n"})
    result = p.evaluate(REPO, apply=True, confirm=SLUG, run=run)
    assert result["status"] == "blocked"
    assert result["issue"] == "ci_gate_missing"
    assert not run.edited


def test_ラチェットの基準値が無ければ公開しない():
    run = FakeRunner(missing={f"{HEAD}:.repo-preflight/ratchet-baseline.json"})
    result = p.evaluate(REPO, apply=True, confirm=SLUG, run=run)
    assert result["status"] == "blocked"
    assert result["issue"] == "ratchet_baseline_missing"
    assert not run.edited


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

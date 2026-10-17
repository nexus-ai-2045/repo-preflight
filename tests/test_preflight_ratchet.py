"""preflight_ratchet の判定。検査本体は差し替え、件数の比べ方だけを確かめる。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import preflight_ratchet as r

ZERO = {name: 0 for name in r.RATCHET_CHECKS}


def report(**overrides):
    checks = {name: {"status": "pass"} for name in r.RATCHET_CHECKS}
    checks["secret_scan"] = {"status": "pass", "finding_count": 0}
    checks["personal_path_scan"] = {"status": "pass", "files": []}
    checks.update(overrides)
    return {"status": "pass", "checks": checks}


def test_件数を検査ごとに数える():
    counts = r.counts_from_report(
        report(
            personal_path_scan={"status": "fail", "files": ["a", "b"]},
            secret_scan={"status": "fail", "finding_count": 3},
            origin={"status": "fail"},
        )
    )
    assert counts["personal_path_scan"] == 2
    assert counts["secret_scan"] == 3
    assert counts["origin"] == 1
    assert counts["clean_worktree"] == 0


def test_unknownは1件として通さない側に倒す():
    counts = r.counts_from_report(report(ci_configuration={"status": "unknown"}))
    assert counts["ci_configuration"] == 1


def test_tool_errorは基準値で見逃さない():
    with pytest.raises(r.RatchetError):
        r.counts_from_report(report(secret_scan={"status": "tool_error"}))
    with pytest.raises(r.RatchetError):
        r.counts_from_report({"status": "tool_error"})


def test_検査が欠けた結果は判定に使わない():
    broken = report()
    del broken["checks"]["origin"]
    with pytest.raises(r.RatchetError):
        r.counts_from_report(broken)


def test_基準値が無ければ全検査0件が基準():
    assert r.evaluate(ZERO, None, None)["status"] == "pass"
    current = dict(ZERO, personal_path_scan=1)
    assert r.evaluate(current, None, None)["status"] == "blocked"


def test_増えたら失敗し減ったら締め忘れで失敗する():
    baseline = dict(ZERO, personal_path_scan=3)
    assert r.evaluate(baseline, baseline, None)["status"] == "pass"
    more = dict(baseline, personal_path_scan=4)
    assert r.evaluate(more, baseline, None)["status"] == "blocked"
    fewer = dict(baseline, personal_path_scan=2)
    assert r.evaluate(fewer, baseline, None)["status"] == "stale_baseline"


def test_比較元より緩めた基準値は失敗():
    base = dict(ZERO, personal_path_scan=2)
    loosened = dict(ZERO, personal_path_scan=3)
    result = r.evaluate(loosened, loosened, base)
    assert result["status"] == "blocked"
    assert result["loosened"][0]["check"] == "personal_path_scan"


def test_締める更新は下げるだけで上げない():
    baseline = dict(ZERO, personal_path_scan=3, secret_scan=1)
    lowered = r.tighten(dict(baseline, personal_path_scan=1), baseline)
    assert lowered["written"] and lowered["counts"]["personal_path_scan"] == 1
    raised = r.tighten(dict(baseline, secret_scan=2), baseline)
    assert not raised["written"]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"schema": "other", "counts": {}}),
        json.dumps({"schema": r.BASELINE_SCHEMA, "counts": {"nope": 1}}),
        json.dumps({"schema": r.BASELINE_SCHEMA, "counts": {"origin": -1}}),
        json.dumps({"schema": r.BASELINE_SCHEMA, "counts": {"origin": True}}),
    ],
)
def test_壊れた基準値は読まない(text):
    with pytest.raises(r.RatchetError):
        r.parse_baseline(text, "baseline")


def test_基準値の書き出しは読み戻せる():
    counts = dict(ZERO, repository_consistency=2)
    assert r.parse_baseline(r.render_baseline(counts), "baseline") == counts


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_比較元refの基準値を読む(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first")
    rel = ".repo-preflight/ratchet-baseline.json"
    assert r.read_baseline_at_ref(tmp_path, "HEAD", rel) is None
    path = tmp_path / rel
    path.parent.mkdir()
    path.write_text(r.render_baseline(dict(ZERO, origin=1)), encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    assert r.read_baseline_at_ref(tmp_path, "HEAD", rel)["origin"] == 1
    with pytest.raises(r.RatchetError):
        r.read_baseline_at_ref(tmp_path, "no-such-ref", rel)


def test_基準値はrepoの外を指せない(tmp_path, capsys):
    assert r.main(["--repo", str(tmp_path), "--baseline", "../x.json"]) == 2

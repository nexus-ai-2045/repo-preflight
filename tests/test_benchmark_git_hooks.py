import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_git_hooks as benchmark


@pytest.mark.parametrize("flag", ["--samples", "--commits"])
def test_cli_rejects_empty_measurements(flag):
    with pytest.raises(SystemExit) as caught:
        benchmark.main([flag, "0"])
    assert caught.value.code == 2


@pytest.mark.skipif(os.name != "posix", reason="fixture executes POSIX shell hooks")
@pytest.mark.parametrize("behavior", ["ok", "fail", "drift"])
def test_cli_measures_each_hook_and_discards_failed_trials(tmp_path, capsys, behavior):
    trace = tmp_path / "trace"
    roots = [tmp_path / label for label in ("first", "second")]
    for root in roots:
        hooks = root / "templates/hooks"
        hooks.mkdir(parents=True)
        for hook in benchmark.HOOKS:
            body = f'echo "{root.name} {hook}" >> "$REPO_PREFLIGHT_HOME/../trace"\n'
            if hook == "pre-push" and root == roots[1]:
                if behavior == "fail":
                    body += "exit 3\n"
                if behavior == "drift":
                    body += 'echo changed >> "$REPO_PREFLIGHT_HOME/templates/hooks/pre-commit"\n'
            (hooks / hook).write_text(body, encoding="utf-8")
    report = tmp_path / "report.json"
    code = benchmark.main(
        [
            "--tool-root",
            str(roots[0]),
            "--compare-root",
            str(roots[1]),
            "--samples",
            "2",
            "--commits",
            "1",
            "--output",
            str(report),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    if behavior != "ok":
        assert code == 2 and payload["status"] == "tool_error"
        assert not report.exists()
        return
    assert code == 0 and json.loads(report.read_text()) == payload
    assert trace.read_text().splitlines() == [
        f"{label} {hook}"
        for label in ("first", "second", "second", "first")
        for hook in ("pre-commit", "commit-msg", "pre-push")
    ]
    for result in payload["results"].values():
        assert all(len(hook["seconds"]) == 2 for hook in result["hooks"].values())

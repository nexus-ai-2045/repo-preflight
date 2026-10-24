"""Run the composite's argument forwarding and input confinement with a stub tool."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="composite action is declared for an Ubuntu bash runner",
)


@pytest.mark.parametrize("mode", ["default", "custom", "absolute", "escape"])
def test_reviewed_identity_file_forwarding_is_confined(tmp_path, mode):
    tool = tmp_path / "tool"
    action = tool / ".github" / "actions" / "preflight"
    action.mkdir(parents=True)
    (tool / "scripts").mkdir()
    (tool / "references").mkdir()
    (tool / "scripts" / "preflight_ratchet.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    target = tmp_path / "target"
    target.mkdir()
    custom = target / "reviewed.json"
    custom.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    value = {
        "default": "",
        "custom": "reviewed.json",
        "absolute": str(outside),
        "escape": "../outside.json",
    }[mode]
    source = (ROOT / ".github/actions/preflight/action.yml").read_text()
    script = textwrap.dedent(source.split("      run: |\n", 1)[1])
    env = dict(
        os.environ,
        PREFLIGHT_ACTION_PATH=str(action),
        TARGET_REPOSITORY=str(target),
        TARGET_BASELINE=".repo-preflight/ratchet-baseline.json",
        TARGET_IDENTITIES=value,
        TARGET_BASE_SHA="",
        TARGET_BASE_BRANCH="",
    )
    run = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    if mode in {"absolute", "escape"}:
        assert run.returncode != 0
        assert not run.stdout
        assert "allowed-identities must be a file inside" in run.stderr
    else:
        assert run.returncode == 0, run.stderr
        args = json.loads(run.stdout)
        path = Path(args[args.index("--allowed-identities") + 1]).resolve()
        assert (
            path
            == (
                custom
                if mode == "custom"
                else tool / "references/public-commit-identities.json"
            ).resolve()
        )

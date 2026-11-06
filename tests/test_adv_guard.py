"""検査対象の名義設定を公開CIが自己承認に使わないことを確認する。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_c2_repo_policy_file_no_longer_widens_identities(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import identity_gate as ig

    (tmp_path / ".repo-preflight").mkdir()
    outsider = "Zz Corp <zzzz@zz-corp.example>"
    (tmp_path / ".repo-preflight/identity-policy.json").write_text(
        json.dumps({"identities": [outsider], "allow_home_users": ["zzzz"]}),
        encoding="utf-8",
    )
    policy = ig.load_public_policy(None, str(tmp_path))
    assert outsider not in policy
    assert "zzzz" not in policy.allow_home_users


def test_c2_ci_workflow_does_not_swap_in_repo_policy():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    assert "identity-policy.json" not in text

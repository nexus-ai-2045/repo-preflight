"""通常のscanner試験には架空の名義policyを渡す。実環境の検査とは区別する。"""

import json

import pytest


@pytest.fixture(autouse=True)
def synthetic_identity_policy(monkeypatch):
    # 実環境の非公開policyを試験へ持ち込まず、失敗条件は各試験で上書きする。
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {
                "allow_email_patterns": [
                    r"[^@]+@example\.invalid",
                    r"(?:\d+\+)?[A-Za-z0-9-]+(?:\[bot\])?@users\.noreply\.github\.com",
                    r"noreply@github\.com",
                ],
                "deny_patterns": ["synthetic-private-identity-marker"],
            }
        ),
    )


@pytest.fixture(autouse=True)
def synthetic_normalized_identity_policy(tmp_path, monkeypatch):
    # 正規化・raw本文gateにも架空入力を明示する。実運用19語やhome配布は使わない。
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    policy = tmp_path / "canonical-test-private-policy.txt"
    policy.write_text("synthetic-private-identity-marker\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))


@pytest.fixture(autouse=True)
def isolated_identity_scan_cache(tmp_path, monkeypatch):
    # 合格控えを試験ごとの一時置き場へ分け、試験間と利用者の控えへ混ぜない。
    monkeypatch.delenv("REPO_PREFLIGHT_IDENTITY_SCAN_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

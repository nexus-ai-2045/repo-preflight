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

"""private provider契約を再現する合成テスト入力。実個人情報は含まない。"""


def configure_private_provider(tmp_path, monkeypatch):
    helper = tmp_path / "identity_provider.py"
    helper.write_text(
        "def parse_denylist_text(text):\n"
        "    values = [v.strip() for v in text.splitlines() if v.strip() and not v.startswith('#')]\n"
        "    if not values: raise ValueError('empty')\n"
        "    return values\n"
        "def scan_private_text(text, patterns):\n"
        "    count = sum(p.lower() in text.lower() for p in patterns)\n"
        "    return {'status': 'fail' if count else 'pass', 'literal_match_count': count, 'local_path_match': False}\n",
        encoding="utf-8",
    )
    policy = tmp_path / "private-policy.txt"
    policy.write_text("fixture-private-identity@example.invalid\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_IDENTITY_GUARD_SCRIPT", str(helper))
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    return helper, policy

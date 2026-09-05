import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / ".claude" / "settings.json"

# remote container の ~/.claude は session ごとに作り直されるため、user scope に
# 置いた設定は残らない。時刻を JST に固定できる durable な層は git 管理下の
# この project settings だけなので、消えたら test で落とす。
EXPECTED_ZONE = "Asia/Tokyo"


def _settings():
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def test_settings_json_is_valid_json():
    # settings.json が壊れると Claude Code はそのファイルの設定を丸ごと
    # 黙って無視する。hook 含め全部落ちるので、構文だけは先に確かめる。
    assert isinstance(_settings(), dict)


def test_tool_env_pins_jst():
    # Bash / date など tool 実行側の時刻。これが無いと UTC のまま返る。
    assert _settings().get("env", {}).get("TZ") == EXPECTED_ZONE


def test_ui_timezone_pins_jst():
    # UI 表示側の時刻。env とは別系統なので両方必要。
    assert _settings().get("timeZone") == EXPECTED_ZONE


def test_session_start_hook_is_preserved():
    # TZ を足すときに hook を落とす事故（merge ではなく replace）を防ぐ。
    hooks = _settings().get("hooks", {}).get("SessionStart", [])
    commands = [
        inner.get("command", "") for entry in hooks for inner in entry.get("hooks", [])
    ]
    assert any("session-start.sh" in command for command in commands)

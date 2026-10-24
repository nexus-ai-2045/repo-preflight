# AI 側の検査ひな形（Claude Code / Codex）

共通フックは、`--no-verify`・`-c core.hooksPath=…`・環境変数（`REPO_PREFLIGHT_HOME`・`PATH`・`PYTHONPATH` など）の上書きで外せます。
人の判断で外すのは止めませんが、AI のシェル実行では `scripts/ai_command_guard.py` が拒みます（exit 2）。

- `claude-settings.json`：Claude Code の settings に足す PreToolUse（Bash）。
- `codex-config.toml`：Codex の config.toml に足す PreToolUse（Bash）。

どちらも `/ABSOLUTE/PATH/TO/repo-preflight` を、承認済み SHA に固定した clone の絶対パスに置き換えてから使います。
このリポにはひな形と検査スクリプトだけを置き、各機械の実際の AI 設定は、人が確かめてから入れます。
base64 などで隠したコマンドまでは見抜けないため、最後の砦は CI の必須チェックです。

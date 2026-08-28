#!/bin/bash
# Claude Code on the web 用の依存導入。
# ローカル開発環境には触れない（CLAUDE_CODE_REMOTE でガード）。
# CI (.github/workflows/ci.yml) と同じ `-e ".[test]"` を入れ、
# pytest / black / runtime smoke が session 開始直後から動く状態にする。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# CLAUDE_PROJECT_DIR は remote でも未設定のことがあるため script 位置から解決する
repo_root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$repo_root"

# pip 自体の upgrade はしない: 環境によっては distro 管理の pip を
# アンインストールできず失敗する。test extras の導入だけが目的。
python3 -m pip install --quiet -e ".[test]"

echo "repo-preflight: test extras installed (pytest, black)."

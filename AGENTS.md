# repo-preflight Agent Rules

作業前に `REPO_GOAL.md` と `.repo-operating-contracts/manifest.json` を確認する。

## Repository identity

- expected remote: `nexus-ai-2045/repo-preflight`
- 書き込み前にrepo root、remote、branch、upstream、ahead / behind、dirty stateを確認する。

## Human review stoplines

現在会話で対象と操作を明示した人間レビューがあるまで、push、PR、release、repository visibility変更、外部送信、hook install、automation enablement、auth / secret / production設定変更を実行しない。

`.repo-operating-contracts/repo-operating-hook.v1.md` は配置のみで、install / enableしていない。

## Verification

```powershell
python .repo-operating-contracts\check.py
```

## 個人名義を出さない（絶対）

- 持ち主の個人名義（本名、私用のメール、OS のアカウント名、端末名、それらを含むホームのパス）を、チャット・PR・Issue・コミット（名義とメッセージ）・ファイル・共有フォルダ・ログのどこにも書かない。確認のためでも書かない。
- 検査が「照合する名前が無い」などで止まっても、ユーザーに名前を書かせる案や、エージェントが値を書き込む案を出さない。照合する値は `local_identity_guard.py`（repo-preflight の `scripts/`）が実行中の環境から自動で集め、表示も保存もしない。
- 値を表示するコマンド（`whoami`、`id -F`、`echo $HOME`、`git config user.email` など）の出力をチャットやファイルへ貼らない。比べる必要があるときは一致・不一致だけを出す。
- コミットの作成者とコミッターは公開用の名義（nexus-ai-2045 の GitHub noreply）だけにする。書き込み前に `python <repo-preflight>/scripts/local_identity_guard.py --public-name nexus-ai-2045 --public-name nexus_ai config` で確かめ、push 前は同じスクリプトの `check` を使う。
- push 前検査や identity guard を緩めない・回避しない（`--no-verify`、`core.hooksPath` の付け替え、照合リストの無効化をしない）。止まったら、値を出さずに原因の箇所を直す。

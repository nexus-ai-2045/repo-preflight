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

# 運用

## Smoke

```powershell
python scripts/readiness_scan.py --repo .
python scripts/readiness_scan.py --repo . --release
python -m pytest -q
```

`status: pass`はローカル自動検査の結果です。公開可否は`publication_decision`と各`unknown`項目を確認し、人が判断します。
release準備では `--release` を省略しません。`readme_release_design` がfailならREADMEを修正し、
再実行します。推奨capabilityは不足箇所へのroutingであり、全pluginの一括起動指示ではありません。

## 更新

scanner pattern、Git probe、状態契約を変更した場合は全testとself-scanを再実行します。GitHub公開後はCI結果、visibility、Security Advisories、rulesetを別途再確認します。

## 障害時

scannerが例外終了、timeout、Git履歴取得失敗になった場合は公開判定を停止します。前回のpassを流用せず、原因修正後に現在HEADで再実行します。

## Rollback

誤判定を含むreleaseは新しい修正commitで戻し、必要なら既知の安全なtagへ案内します。公開前には、一時リポジトリで直前の安全なcommitへ戻してテストとCLI起動を確認する復旧訓練を行い、対象commitと結果を記録します。repository visibility変更は自動化せず、GitHub上の別手順と人間承認で扱います。

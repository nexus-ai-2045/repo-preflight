# 成果物契約

必要なものだけ作成し、存在だけで合格にしない。

- `README.md`: Why、What、How、制約、quickstart
- `LICENSE`とthird-party notice: code、data、model、画像、fontの権利
- `SECURITY.md`: 非公開報告経路とdata handling
- `CONTRIBUTING.md`: setup、test、sample data方針
- `docs/architecture.md`: componentとtrust boundary
- `docs/threat-model.md`: asset、attacker input、invariant、failure mode
- `OPERATIONS.md`: install、smoke、monitor、rollback、再検証条件
- `PUBLIC_READY.md`: 検査対象HEADと検査日時を含むreview記録。この記録を追加する後続commitでは、検査対象HEADと文書commitを分けて明示する
- `readiness-report.json`: pass/fail/unknown/not_applicableとevidence。対象リポジトリ側または非公開の運用記録へ保存し、このツールのリポジトリへ個別案件の結果をcommitしない
- `cleanup-report.md`: merged確認、削除候補、残務

テンプレートは`assets/`を参照し、既存ファイルを無断で上書きしない。

# 成果物契約

必要なものだけ作成し、存在だけで合格にしない。

- `README.md`: H1直後の短い価値説明、Why、What、quickstart、制約を理解順序どおりに置く。原則300行以内とし、詳細はdocsへ分離する。release準備では `readme_release_gate.py` の証拠を残す
- `LICENSE`とthird-party notice: code、data、model、画像、fontの権利
- `SECURITY.md`: 非公開報告経路とdata handling
- `CONTRIBUTING.md`: setup、test、sample data方針
- `docs/architecture.md`: componentとtrust boundary
- `docs/threat-model.md`: asset、attacker input、invariant、failure mode
- `OPERATIONS.md`: install、smoke、monitor、rollback、再検証条件
- `PREFLIGHT.md`: 検査対象HEADと検査日時を含むreview記録。先頭に `<!-- repo-preflight:review-record -->` を置く（scannerはこのmarkerでdeployment preflight等の無関係な同名fileと区別する）。この記録を追加する後続commitでは、検査対象HEADと文書commitを分けて明示する
- `readiness-report.json`: pass/fail/unknown/not_applicableとevidence。対象リポジトリ側または非公開の運用記録へ保存し、このツールのリポジトリへ個別案件の結果をcommitしない
- `cleanup-report.md`: merged確認、削除候補、残務

テンプレートは`assets/`を参照し、既存ファイルを無断で上書きしない。

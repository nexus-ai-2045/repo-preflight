# 運用

## Smoke

最短1本（自己・impact_map 付きでも整合性は既定の `origin/main` を使う）:

```powershell
python scripts/readiness_scan.py --repo .
```

release 準備と runtime / テスト:

```powershell
python scripts/readiness_scan.py --repo . --release
python scripts/runtime_smoke.py --repo .
python scripts/local_check.py --run-selected-tests --base-ref origin/main --json
```

Claude Code / Grok へ skill pointer を配る (書込は `--apply`):

```powershell
python scripts/install_runtime_skills.py --repo .
python scripts/install_runtime_skills.py --repo . --apply
```

保証境界: [docs/runtime-support.md](docs/runtime-support.md)

`status: pass`はローカル自動検査の結果です。公開可否は`publication_decision`と各`unknown`項目を確認し、人が判断します。
release準備では `--release` を省略しません。`readme_release_design` がfailならREADMEを修正し、
再実行します。推奨capabilityは不足箇所へのroutingであり、全pluginの一括起動指示ではありません。

浅い clone では履歴個人pathが無く `pass` になり得ます。完全履歴では `personal_path_scan` で `blocked` になり得ますが、それは別件です。整合性の `tool_error`（`change_sensitive_scope_unavailable`）にはしない、というのがこの Smoke の契約です。

## 手元の検査の範囲

- 手元もCIも `scripts/local_check.py` の差分・逆依存・文字参照による同じ選択入口を使う。手元は `python scripts/local_check.py`、計画だけなら `--plan-tests --json`。比較元は `--base-ref` で指定する。`full` / `quick` は既存呼出し用の互換名で、試験対象はどちらも差分から選ぶ。`full` は従来の追加ローカルgateも維持する。
- repo所有の `.test-selection.json` がsource/test rootsと周辺へ広げる明示ruleを定める。依存・CIの変更も対応するtest familyと周辺を指定し、全件への暗黙fallbackを使わない。conftest.pyの変更は暗黙fixtureの利用範囲を推測せず、明示mappingがあってもfixture-impact unresolvedで停止する。所有者の範囲reviewが必要。対応関係が不明、またはruleが存在しないtestを指定した場合はunresolvedで停止し、所有者がmappingを直す。PR前・merge前も全件試験を既定にしない。ruleの `dependency_mode` は既定 `auto`。同じ変更にマッチする全ruleが有効な処置付きで `mapped` を明示した場合だけowner指定範囲を使い、自動参照拡張を省く。autoとの重複では拡張を維持し、変更test自身は常に含める。CoreのmappedはREADME等の一般metadataのexact allowlistのみ。CHANGELOGとdocs/referencesの契約文書は自動参照拡張を維持する。
- `--plan-tests --repo TARGET --selection-config POLICY --json` は対象repoのpolicyだけを受け、対象・理由・未解決・check IDを返す。check IDと言語・OSごとのrunnerは対象repoが所有する。`--run-selected-tests` はpytest専用で、空planをpytestの全件実行へ変換しない。CLIは出力前に環境由来のprivate identityと既存のpath伏せ字対象を検査し、該当planはpathを含まないunresolved（exit 2）へ置換して実行しない。test policyは名義許可を変更できない。in-process APIのraw pathは保持する。
- 名義と個人情報の検査（`identity_gate --public`）は、比較元からHEAD、ステージ、未ステージ・未追跡の全差分を見る。一時索引を使い、本物のステージ状態は変えない。secret/identity/ratchet/base保証・required check名は維持する。Linux/Mac/Windows、Python 3.9/3.11/3.13はpath処理とCLI・hook互換性の既存保証であり、今回も選択した試験を必要な環境で回す。CI結果とローカル結果は別に確認する。
- 画面には合否・件数・失敗した項目名だけを出し、全文のログは `.git/local-check/` に落とす。
- release 準備の README 更新の検査（`--intent release` / `--release`）は、このモードとは別に、今までどおり省略しない。

## リリース

1. 版上げの PR で `pyproject.toml` の version、`CHANGELOG.md` 先頭の `## X.Y.Z - YYYY-MM-DD`（日付は JST）、リリースノート `docs/releases/vX.Y.Z.md`（1行目が `# タイトル`）をそろえて merge する。
2. リリース対象の main commit と版について人間レビューを受け、その commit を指す `vX.Y.Z` タグを承認済みの別手順で用意する。`v*` は保護対象であり、この workflow はタグを作成・更新せず、保護設定も変更しない。タグ作成が拒否される場合は、権限や保護設定の判断を管理者へ戻し、workflow の再実行で代替しない。
3. Actions の `release` workflow を main で手動起動し、`version` に `X.Y.Z` を渡す。版の不一致・タグの不足・タグと実行commitの不一致があれば、ブランチ削除やRelease作成の前に止まる。軽量タグと注釈付きタグは、どちらも参照先commitを照合する。`--verify-tag` により、Release作成時のタグ自動作成も禁止する。既存Releaseは上書きしない。
4. 不要なリモートブランチは `delete-branches` に空白区切りで渡すと、リリース前に消す。開いた PR があるブランチは消さずに止まる。

## 更新

scanner pattern、Git probe、状態契約を変更した場合は全testとself-scanを再実行します。GitHub公開後はCI結果、visibility、Security Advisories、rulesetを別途再確認します。

## 障害時

scannerが例外終了、timeout、Git履歴取得失敗になった場合は公開判定を停止します。前回のpassを流用せず、原因修正後に現在HEADで再実行します。

## Rollback

誤判定を含むreleaseは新しい修正commitで戻し、必要なら既知の安全なtagへ案内します。公開前には、一時リポジトリで直前の安全なcommitへ戻してテストとCLI起動を確認する復旧訓練を行い、対象commitと結果を記録します。repository visibility変更は自動化せず、GitHub上の別手順と人間承認で扱います。

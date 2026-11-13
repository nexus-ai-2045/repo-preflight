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
python scripts/local_check.py --ci --base-ref origin/main --json
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

自己リポの7本のCLI試験（6 intentと引数なし）は、現在HEADの全追跡tree・実行権限を保つ一時Gitリポジトリで実行します。履歴は同じtreeの2 commit、`origin/main` はHEADと同じで、空diffと初回commit以外の整合性検査を実Git・実scannerで確かめます。Linux / Mac / Windows の既存互換ジョブを維持し、同じ開発履歴の重複走査だけを減らします。productionの全履歴検査は変更しません。

実checkoutの全履歴release試験は、この自己試験が差分・依存から選択された時にLinux / Python 3.11の既存pytest stepで1回実行します。無関係な変更では選択範囲を広げません。浅いcheckoutや使用不能な`origin/main`はCIで失敗します。手元で同じ検査を実行する場合は環境変数`REPO_PREFLIGHT_DEEP_SELF_TEST=1`を設定し、`python -m pytest tests/test_self_intents.py -k test_real_checkout_release -q`を実行します。未指定時はこの1件だけskipします。

## 手元の検査の範囲

- 手元もCIも `scripts/local_check.py` の差分・逆依存・文字参照による同じ選択入口を使う。手元は `python scripts/local_check.py`、計画だけなら `--plan-tests --json`。比較元は `--base-ref` で指定する。`full` / `quick` は既存呼出し用の互換名で、試験対象はどちらも差分から選ぶ。`full` は従来の追加ローカルgateも維持する。
- リポジトリの `.test-selection.json` で検査対象を定める。通常は変更されたテストと逆依存のテストを選ぶ。`supplement` は逆依存を維持したまま指定テストを加える。Coreの動的ロードを含む名義・メディア・AIガード試験には、その試験をヘルパーとして使うテストも指定している。対応する有効な処置がすべて `mapped` の変更は所有者の指定範囲を使う。判定時は追加専用の `supplement` を除く。単独CLIの `benchmark_git_hooks.py` と `ai_command_guard.py` は専用テストを指定し、静的にたどれる利用側のテストがすべて選ばれることも検査する。
- 未知の対応や `conftest.py` の変更は計画で `unresolved` として返す。手元の通常検査とCIの `--ci` は停止する。`--ci` は選択実行と自動並列をまとめた入口で、未知・private identity・未処理check IDがあればexit 2を返し、pytestを起動しない。既知の空集合は成功skipにする。`--plan-tests` / `--dry-run` / `--identity-worktree` とは併用できない。
- 互換入口の `--run-selected-tests` は従来どおり、未知ならポリシー内の全テストと選択済みテストを合わせて実行する。いずれの実行も変更テストを先頭に置き、各ファイルを一度だけpytestへ渡し、最初の失敗で打ち切る。明示の `--pytest-workers auto` または `1`〜`256` を優先し、未指定ならCIと互換の全件切り替えは自動並列、通常の選択実行は直列になる。
- CIの標準出力は固定schema、選択状態、実行判断、件数、検証済みSHAだけを返し、file/path/pattern/refnameを含めない。`GITHUB_STEP_SUMMARY` があれば同じ安全な情報をMarkdownで追記する。summaryを書けなければ固定理由 `summary_write_failed` で停止し、pytestを起動しない。`--plan-tests --json` の詳細plan形式は維持する。
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

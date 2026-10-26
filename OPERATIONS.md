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
python -m pytest -q
```

Claude Code / Grok へ skill pointer を配る (書込は `--apply`):

```powershell
python scripts/install_runtime_skills.py --repo .
python scripts/install_runtime_skills.py --repo . --apply
```

保証境界: [docs/runtime-support.md](docs/runtime-support.md)

`status: pass`はローカル自動検査の結果です。公開可否は`publication_decision`と各`unknown`項目を確認し、人が判断します。
release準備では `--release` を省略しません。`readme_release_design` がfailならREADMEを修正し、
再実行します。README に図区画の印（`<!-- diagram:start -->` / `<!-- diagram:end -->`）があるときは
`python scripts/readme_diagram.py --repo . --apply` で構成・流れの mermaid を埋めます（印が無ければ何もしません）。
推奨capabilityは不足箇所へのroutingであり、全pluginの一括起動指示ではありません。

浅い clone では履歴個人pathが無く `pass` になり得ます。完全履歴では `personal_path_scan` で `blocked` になり得ますが、それは別件です。整合性の `tool_error`（`change_sensitive_scope_unavailable`）にはしない、というのがこの Smoke の契約です。

## 手元の検査の範囲

- 途中の作業（commit・push の前など）は `python scripts/local_check.py --mode quick` を使う。変えたファイルに関係する試験・black・文書検査だけを回す。関係する試験が決まらない変更（試験の共有部品・依存・設定・`.github` など）は、安全側で全部の試験に戻す。quick は CI の代わりではない。
- PR を出す前とマージ前は `python scripts/local_check.py`（既定 `--mode full`）で、CI が回す試験と検査のうち手元で回せるものを全部回す。PR 本文の検査（`--pr-event`）・`base_guard`・全 ref の走査は GitHub 側でしか回らないので、full でも回らない。full が緑でも CI の結果は別に確かめる。
- 名義と個人情報の検査（`identity_gate --public`）は、どのモードでも、比較元から HEAD までの全差分、ステージした差分、まだステージしていない変更と未追跡のファイル（作業ツリー）の全部を見る。作業ツリーは一時の索引に載せて見るので、本物のステージの状態は変わらない。GitHub の必須チェックと CI の中身は変えない。
- 画面には合否・件数・失敗した項目名だけを出し、全文のログは `.git/local-check/` に落とす。
- release 準備の README 更新の検査（`--intent release` / `--release`）は、このモードとは別に、今までどおり省略しない。

## リリース

1. 版上げの PR で `pyproject.toml` の version、`CHANGELOG.md` 先頭の `## X.Y.Z - YYYY-MM-DD`（日付は JST）、リリースノート `docs/releases/vX.Y.Z.md`（1行目が `# タイトル`）をそろえて merge する。
2. Actions の `release` workflow を main で手動起動し、`version` に `X.Y.Z` を渡す。版の不一致や既存 tag があれば何も作らずに止まる。
3. 不要なリモートブランチは `delete-branches` に空白区切りで渡すと、リリース前に消す。開いた PR があるブランチは消さずに止まる。

## 更新

scanner pattern、Git probe、状態契約を変更した場合は全testとself-scanを再実行します。GitHub公開後はCI結果、visibility、Security Advisories、rulesetを別途再確認します。

## 障害時

scannerが例外終了、timeout、Git履歴取得失敗になった場合は公開判定を停止します。前回のpassを流用せず、原因修正後に現在HEADで再実行します。

## Rollback

誤判定を含むreleaseは新しい修正commitで戻し、必要なら既知の安全なtagへ案内します。公開前には、一時リポジトリで直前の安全なcommitへ戻してテストとCLI起動を確認する復旧訓練を行い、対象commitと結果を記録します。repository visibility変更は自動化せず、GitHub上の別手順と人間承認で扱います。

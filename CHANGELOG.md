# Changelog

## 0.2.0 - 2026-08-01

### 破壊的変更

- package名、skill名、agent interface keyを`public-readiness`から`repo-preflight`へ変更。旧名は永久欠番とし、再利用しない。
- 必須成果物を`PUBLIC_READY.md`から`PREFLIGHT.md`へ改名。`REQUIRED`が検査対象repositoryに要求するファイル名が変わるため、既存の`PUBLIC_READY.md`は改名が必要。
- README release gateのschema識別子を`repo-preflight.readme-release-gate/v1`へ変更。

旧名は「public化がゴール」と読めるが、private保存、PRまで、mergeまでを正規の完了地点として扱う本来の設計と矛盾していた。検査項目は公開可否と無関係に効くため、チーム共有、客先納品、外部協力者への受け渡しでも同じ手順を通す。

### 追加

- release準備時にREADMEの短さ、理解順序、Quickstart、制約、見出し階層を検査するread-onlyゲート。
- `readiness_scan.py --release` からREADME設計ゲートを自動実行する経路。
- READMEの不足内容に応じて必要なデザインskill/pluginだけを提案するrouting。
- 公開repositoryのruleset、merge方式、Actions権限、security機能を選ぶための設定ガイド。

### 修正

- git identityが未設定の環境で全scanが`tool_error`終了していた問題。現在名義のprobeは`--expected-identity`指定時のみ実行し、失敗時はscan全体を落とさず`commit_identity`を`unknown`として返す。CI container上での実行が回復する。
- `i18n.logOutputEncoding`が非UTF-8のrepositoryで作者名が壊れ、誤ったidentity mismatchを報告していた問題。git側の出力encodingをUTF-8に固定してから読む。
- 片方のidentity probeが失敗したとき、成功した側が示すmismatchまで`unknown`へ丸めていた問題。probeを個別に評価し、既知の不一致を隠さない。
- 予期しない例外が終了コード1を返し、`blocked`と区別できなかった問題。`tool_error`として終了コード2へ写像し、path/secretを含み得る例外messageは出力しない。
- Windowsのcp932環境でgit出力が文字化けし得た問題。subprocess出力をUTF-8として明示的にdecodeし、stdoutもUTF-8にする。

### 改善

- secret scanの検出結果に秘密値そのものを表示しない回帰防止。
- release gateのpassと、人間レビュー・release承認を別状態として維持。
- READMEとSKILL.mdに、public化が到達点のひとつであり唯一の終点ではないことを明記。
- package versionとCHANGELOG先頭見出しの整合テストが、古い見出しの残存で通過し得た点を修正。先頭のrelease見出しを解析して比較する。

## 0.1.0 - 2026-07-22

- Gitリポジトリの公開準備をread-onlyで検査する初回release。

# Changelog

## Unreleased

### 追加

- 日本語READMEのクイックスタートを、AIへ貼る URL と危険レビュー指示にする契約を warning として検知する。
- 図が画像自身ではなく ADR / 再現テストへリンクされているかを warning として検知する。

## 0.5.0 - 2026-08-17

### 追加

- README可読性ゲートの判定を、公開を止めるエラーと人間確認へ回す警告に分離した。
- Mermaid図の日本語化候補を `Localize Diagram` として、図の追加候補とは別に提示するようにした。
- GitHub Actionsの完全SHA更新だけを限定的に免除するconsistency gateと、判断根拠を記録するADR-0001を追加した。
- DependabotのGitHub Actions向けversion updates設定を追加した。

### 修正 / 改善

- 名前付きstep、reusable workflow、版コメントを伴うAction SHA更新を構造的に判定するよう改善した。
- file mode変更、symlink化、`env`・`run`内の疑似`uses`、YAML flow collection・property・複数行scalar、workflow配下のtemplateを免除しないfail-closed判定へ強化した。
- READMEの表幅、Mermaid label抽出、fence判定、全件報告を改善し、修正と再検査の往復を減らした。
- CIで利用する`actions/checkout`を7.0.1、`actions/setup-python`を7.0.0へ更新し、参照は完全SHAで固定した。

### 保証境界

- 保証: 通常のworkflow構造にある同一Actionまたはreusable workflowの完全SHAだけが変わる場合に限り、明示opt-inされた関連文書・テスト要件を免除する。
- 非保証: 任意のYAML構文を完全解析すること、Action更新の安全性そのもの、GitHub設定・merge・releaseを自動承認すること。

## 0.4.0 - 2026-08-16

### 追加

- `--base-ref`（`push` / `open_pr` / `merge`）により、指定した差分範囲へ検査対象を限定できる preflight mode を追加 (#14)。結果 JSON の `scan_scope.mode` は内部値 `target_diff`。CLI フラグ名としての `--target-diff` はない。
- repository 全体の文書・設定・実装の食い違いを検出する consistency gate を追加 (#15)。
- README の理解順序、表の幅、Mermaid 図のラベルを含む日本語可読性ゲートを追加 (#16, #19)。

### 修正 / 改善

- README を日本語で読みやすい構成へ整理し、詳細説明を `docs/` へ分離 (#13, #17, #18)。
- 空 diff、改行を含む path、浅い・特殊な Git 履歴でも history inventory が安全に完了するよう修正 (#17, #20)。
- sparse checkout では、実際に materialize された `skip-worktree` file を検査対象に含めるよう修正 (#20)。
- consistency JSON と symlink fixture を Windows / Linux / macOS 間で再現可能にした (#14, #15)。

### 保証境界

- 保証: 対象差分と repository 全体の検査範囲を分離し、read-only の scan / consistency / README gate を CI で検証する。
- 非保証: GitHub 設定の自動変更、公開・merge・release の自動承認、依存関係の脆弱性が存在しないこと。

## 0.3.2 - 2026-08-06

### 修正 / 改善

- skill に絶対 path を焼かない portable install に変更。
  - install 先に `run_preflight.py` + `checkout/` link を置く
  - root 解決: `REPO_PREFLIGHT_ROOT` → `checkout/` → cwd 探索
- 他人の skill フォルダをコピーすると壊れる問題を設計上避ける（各自 `install_runtime_skills.py --apply`）
- Claude Code / Grok adapter と runtime-support 文書を更新

## 0.3.1 - 2026-08-06

### 追加

- Claude Code / Grok / Codex 向け runtime adapter と保証境界文書 (`docs/runtime-support.md`)。
- `scripts/runtime_smoke.py`: CLI 対話契約 + skill 入口の最小 smoke。
- `scripts/install_runtime_skills.py`: ホーム skills への pointer 配布 (dry-run 既定、`--apply` で書込)。
- CI を `ubuntu-latest` + `macos-latest` の matrix に拡大し、runtime smoke を必須化。

### 保証境界 (明示)

- 保証: 同一 CLI 契約が Linux/macOS CI で通る。Claude Code/Grok adapter が正本 SKILL を指す。
- 非保証: 各製品が skill を自動導入すること、モデルが skill を無視しないことの物理強制。

## 0.3.0 - 2026-08-06

### 追加

- **AI 実装フロー向け intent 対話ゲート** (`--intent create_repo|push|open_pr|merge|publish|release`)。
  エージェントが repo 作成 / push / PR / merge / 公開 / release に進む直前に自動発火し、
  不足文書・未設定・推奨設定を「設定しますか？」形式の `proposals` / `confirmations` として返す。
- dialogue schema `repo-preflight.dialogue/v3` と `scripts/dialogue_gate.py`。
- **次から出さない / snooze**: 推奨質問に `dismiss_30d` / `dismiss_90d` / `dismiss_forever` を付与。
  採用先 `.repo-preflight.json` に記録 (`--record-dismissal`)。secret 等 fail-closed は抑止不可。
- **GitHub 設定ガイド鮮度**: `references/github-settings.md` の `last_reviewed` 期限切れを検知し、
  更新確認の proposal を出す。リアルタイム自動追従は非保証、鮮度検知と更新確認は保証。
- 保証すること / 保証しないことを scan と dialogue の両方に常に同梱する。
- scan schema `repo-preflight.scan/v3`、`--human`、`--audience`。
- コンソール補助の `--interactive` (本体は intent 対話。TTY メニューは任意)。

### 互換

- 素の検査 (`--repo` のみ、stdout JSON) は維持する。
- `status: pass` と `publication_decision: blocked_human_review_required` の分離は変えない。
- secret 検出時に ignore 選択肢を出さない。
- 旧 `--json` は引き続き受け付けない (v0.2.0 と同様)。

## 0.2.0 - 2026-08-01

### 破壊的変更

- package名、skill名、agent interface keyを`public-readiness`から`repo-preflight`へ変更。旧名は永久欠番とし、再利用しない。
- 必須成果物を`PUBLIC_READY.md`から`PREFLIGHT.md`へ改名。`REQUIRED`が検査対象repositoryに要求するファイル名が変わるため、既存の`PUBLIC_READY.md`は改名が必要。
- README release gateのschema識別子を`repo-preflight.readme-release-gate/v1`へ変更。
- `readiness_scan.py`から`--json`を削除。定義されていたがどこからも参照されておらず、「付けないとJSONにならない」という誤解を生んでいた。出力は以前から常にJSONであり、実際の挙動は変わらない。`--json`を付けて呼んでいた場合はexit 2になるため、フラグを外す。

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

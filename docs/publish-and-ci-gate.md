# 公開コマンドと共通 CI ゲート

repo-preflight を「必ず通る」形にするための3点セットです。GitHub には公開範囲の切り替えそのものを止める仕組みが無いため、**公開は専用コマンドから**、**日々の変更は CI ゲートで**、**既存の指摘はラチェットで**押さえます。

| 押さえる場面 | 仕組み | 入口 |
|---|---|---|
| private → public の切り替え | 公開コマンド | `scripts/publish_gate.py` |
| push・PR・merge | 共通 CI ゲート | `.github/workflows/preflight-gate.yml` |
| 既存 repo の指摘 | ラチェット | `scripts/preflight_ratchet.py` |
| AI エージェントの直接公開 | 公開ガードのフック | `scripts/publish_guard_hook.py` |

> [!IMPORTANT]
> CI ゲートだけでは「CI は緑だけど公開ボタンは素通り」になります。公開は必ず公開コマンドから行うと運用で決めてください。GitHub の設定画面や `gh repo edit` を直接使えば、このコマンドを経ずに公開できてしまいます。

## 1. 公開コマンド

```bash
# 何も変えずに判定だけ（既定）
python scripts/publish_gate.py --repo /path/to/clone

# 人が公開を決めたあと。対象名をもう一度書く
python scripts/publish_gate.py --repo /path/to/clone --apply --confirm OWNER/NAME
```

次をすべて満たしたときだけ `gh repo edit OWNER/NAME --visibility public` を実行し、切り替え後に GitHub 上で `PUBLIC` になったことを確かめます。

1. origin が GitHub で、`--github OWNER/NAME` を付けたならそれと一致する
2. 手元が origin の default branch と同じ commit で、origin の全 branch / tag を取得済み（検査した履歴＝公開される履歴）
3. `readiness_scan.py` の全体検査（削除済みを含む全履歴）が `pass`
4. HEAD に共通 CI ゲート（`preflight-gate.yml` を `uses:` で呼ぶ workflow、「2.」）と、その基準値ファイル（既定 `.repo-preflight/ratchet-baseline.json`、`baseline:` を渡していればその path）がある。無ければ `ci_gate_missing` / `ratchet_baseline_missing` で止まる。公開した後の push と PR でも検査を走らせ続けるため
5. `--apply` と、対象名を打ち直した `--confirm OWNER/NAME` がある
6. 切り替えの直前に origin が動いていない

**ラチェットの基準値は使いません。** 基準値で CI を通している既存の指摘（履歴に残った個人の path など）があると、公開コマンドは止まります。CI が緑でも公開できるとは限らない、という点に注意してください。

全体検査には名義検査（`identity_gate`）も含まれるため、公開コマンドを実行する手元には非公開設定（`PREFLIGHT_PRIVATE_PATTERNS_FILE` または `PREFLIGHT_PRIVATE_PATTERNS`）が要ります。無ければ止まります。

レビュー済みの例外を使う repo は `--reviewed-secret-exceptions` をそのまま渡せます。

| `status` | 意味 | 終了コード |
|---|---|---|
| `ready` | dry-run で全部通った。まだ何も変えていない | `0` |
| `published` | 公開に切り替え、`PUBLIC` を確認した | `0` |
| `already_public` | もともと公開。何もしていない | `0` |
| `blocked` | 条件を満たさない。`issue` と `failed_checks` を見る | `1` |
| `tool_error` | 確認できなかった。判断に使わない | `2` |

`gh` が認証済みで、対象 repository の admin 権限があることが前提です。

## 2. 共通 CI ゲート

各 repository に次の1ファイルを置くだけで導入できます。フォークやコピーは要りません。

```yaml
# .github/workflows/preflight.yml
name: preflight
on:
  pull_request:
  merge_group:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  gate:
    uses: nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml@<tag か commit SHA>
    with:
      preflight-ref: <uses の @ と同じ値>
```

- 呼び出し側の repository を全履歴で取得し、repo-preflight は別の場所へ取得するので、検査対象の作業ツリーは汚れません。
- `preflight-ref` には `uses:` の `@` と同じ値を渡してください。違うと、workflow と検査スクリプトの版がずれます。
- 基準値ファイルの場所を変えるときは `baseline:` を渡します（既定 `.repo-preflight/ratchet-baseline.json`）。

### 通らないとマージできないようにする

repository の Settings → Rules（または Branches）で default branch の保護に**必須チェック**を追加し、`gate / preflight` を選びます。名前は「呼び出し側の job id / preflight」です。job id を `gate` 以外にしたときはそれに合わせます。一度 CI を走らせてからでないと候補に出ません。

## 3. ラチェット

CI ゲートは `preflight_ratchet.py` で判定します。検査ごとの指摘件数を基準値ファイルと比べ、**減るのはよいが増えたら失敗**にします。

| `status` | 意味 | 終了コード |
|---|---|---|
| `pass` | 基準値から増えていない | `0` |
| `blocked` | 増えた検査がある、または PR で基準値を緩めた | `1` |
| `stale_baseline` | 減ったのに基準値を締めていない | `1` |
| `tool_error` | 検査できなかった。基準値で見逃さない | `2` |

基準値ファイルが無い repository は、全検査 0 件が基準です。指摘がある既存 repository へ導入するときは、手元で基準値を作り、人がレビューしてから commit します。

```bash
python /path/to/repo-preflight/scripts/preflight_ratchet.py --repo . --update-baseline
```

- `--update-baseline` は基準値を作るか**下げる**だけです。増えた検査があると書きません。
- 指摘を減らした PR では、同じ PR で `--update-baseline` を実行して締めます。締め忘れは `stale_baseline` で落ちます。
- PR では比較元の基準値より緩めていないかも確かめます（`--baseline-base-ref`）。基準値を上げる変更は CI で落ちます。比較元に基準値が無い初回導入だけは、レビューで値を決めます。
- 件数だけを比べます。同じ検査の中で指摘が入れ替わっても件数が同じなら気づけません。
- 基準値ファイルを書いた直後は作業ツリーが汚れるため、commit してから再実行してください。

数える検査は `clean_worktree`、`required_documents`、`secret_scan`、`personal_path_scan`、`commit_identity`、`dependency_configuration`、`ci_configuration`、`origin`、`repository_consistency` です。`identity_gate` は数えません。CI には非公開設定を置かないためで、名義は手元の検査と公開コマンドで押さえます。件数欄が無い検査は `fail` か `unknown` を1件と数えます。

## 4. AI エージェントの直接公開を止めるフック

公開コマンドを使う運用を、Claude Code が操作する手前で機械的に守らせます。`scripts/publish_guard_hook.py` は PreToolUse フックで、次の操作を止めて公開コマンドへ案内します。

- `gh repo edit` での公開への切り替え、`gh repo create` での公開 repository の作成
- `gh api` や `curl` から GitHub API へ非公開の解除を送る操作
- GitHub MCP の `create_repository` で、非公開を明示していない作成（既定は公開）

公開コマンドは内部で `gh` を呼ぶため、このフックからは見えずにそのまま通ります。標準ライブラリだけで書いてあり、入出力は UTF-8 で扱うので、macOS・Linux・Windows で同じように動きます（CI で3つの OS のテストを通しています）。

repo-preflight の clone で次の1行を実行すると、ユーザー設定（`~/.claude/settings.json`、Windows は `%USERPROFILE%\.claude\settings.json`）に足します。すべての repository とセッションに効きます。既存の設定は残し、元のファイルは `settings.json.bak` に保存します。2回目以降は何もしません。

```bash
python3 scripts/publish_guard_hook.py --install   # Windows は py -3 scripts/publish_guard_hook.py --install
```

手で書く場合は、次の形を `hooks` に足します。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell|mcp__.*github.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/repo-preflight/scripts/publish_guard_hook.py"
          }
        ]
      }
    ]
  }
}
```

Windows では `command` を `py -3 C:\path\to\repo-preflight\scripts\publish_guard_hook.py` にします（JSON の中では `\` を `\\` と重ねて書きます）。設定後は、Claude Code に公開 repository の作成を頼むと止まることを確かめてください。

止められないもの: 人が GitHub の画面や自分の端末で直接行う公開、Claude Code 以外のエージェント（同じ形のフックを持つものは同じスクリプトを使えます）。フックの入力を読めないときは止めずに通します。

設計判断は [ADR-0009](adr/0009-publish-gate-and-ratchet.md) にあります。

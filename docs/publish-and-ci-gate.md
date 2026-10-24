# 公開コマンドと共通 CI ゲート

repo-preflightの失敗を後続操作の中止につなげる導入手順です。**公開は専用コマンドから**、**マージの制限はCIと必須チェックの設定で**、**既存の指摘の増加はラチェットで**確認します。設定や呼出経路を含めて導入先で検証する必要があり、cloneやworkflowの追加だけで全操作が止まるとは扱いません。

| 押さえる場面 | 仕組み | 入口 |
|---|---|---|
| private → public の切り替え | 公開コマンド | `scripts/publish_gate.py` |
| CIの失敗と保護対象branchへのマージ制限 | 共通CIと必須チェックの設定 | `.github/workflows/preflight-gate.yml` |
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
    types: [opened, synchronize, reopened, edited]
  merge_group:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  gate:
    uses: nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml@<承認済み commit SHA> # vX.Y.Z（保護したタグ）
    with:
      preflight-ref: <uses の @ と同じ40桁SHA>
```

- 呼び出し側の repository を全履歴で取得し、repo-preflight は別の場所へ取得するので、検査対象の作業ツリーは汚れません。
- `preflight-ref` は必須です。`uses:` の `@` と同じ40桁の commit SHA を渡してください。枝名やタグ名、既定値はありません（SHA でないと gate が赤になります）。違う SHA だと、workflow と検査スクリプトの版がずれます。
- そろっているかは base-guard（`scripts/base_guard.py`）が、比較元（保護された側）の定義で毎回確かめます。PR 側で書き換えた定義ではなく、比較元に入っている定義を見ます。ずれ・枝名やタグ・`preflight-ref` の欠落は赤になります。比較元に呼び出しがまだ無い導入の PR と初回 push は見送ります。手元では `python3 scripts/base_guard.py --repo . --base-ref origin/main` で同じ判定を見られます。repo-preflight 本体だけは、PR の版の gate で自分を検査するため `./.github/workflows/preflight-gate.yml` を `preflight-ref: ${{ github.sha }}` で呼びます。この形は `--self-repository`（CI では `github.repository`）が本体のときだけ通し、ほかの repo では手元の写しとして赤にします。
- `@` に書く commit SHA は、repo-preflight の `main` に入っている commit（またはタグ）にしてください。PR ブランチの SHA は squash merge と枝の削除のあと clone から取れなくなり、検査スクリプトを取得する段階で止まります。
- 基準値ファイルの場所を変えるときは `baseline:` を渡します（既定 `.repo-preflight/ratchet-baseline.json`）。
- 比較元はPRの`base.sha`、merge queueの`base_sha`、通常pushの`before`へ固定します。originから同じSHAをremote refへ取得し、基準値の緩和検査と整合性検査を同じcommitへ束縛します。イベント後にbranchが進んでも比較元を置き換えず、`master`を`origin/main`の既定値へ誤って戻しません。
- checkoutの`persist-credentials: false`は維持します。非公開の利用側でも比較元を取得できるよう、組込み`github.token`（`contents: read`）の認証headerをそのfetch一命令の環境だけへ渡します。credential helperとtraceを抑え、tokenをGit設定ファイルへ保存せず、raw tokenや認証設定を後続の検査processへ渡しません。追加Secretsや権限拡張は不要です。
- 比較元の欠落・不正・非祖先、checkoutとイベントHEADの不一致、force push、削除通知は検査本体へ進めず停止します。初回pushのゼロ比較元だけは、既存の`initial_tree`契約（非shallowかつ全参照で単一root commit）を確認して全体検査します。空commitや架空の比較元は作りません。PR・merge queue・push以外からの呼出しは未対応として停止します。

### 全 ref の定期検査（repo-preflight 本体）

PR・push の CI は、そのイベントの commit から辿れる履歴だけを見ます。PR の無い枝・タグ・閉じた PR の head は、本体では `.github/workflows/all-refs-scan.yml` が毎日（03:17 UTC）と手動実行で見ます。

- ref（枝・タグ・`refs/pull/*/head`）ごとに、その ref の履歴とデフォルト枝の commit だけを入れた別の作業場を作り、`identity_gate.py --public`・`preflight_ratchet.py`・固定版 gitleaks を呼びます。新しい検査はありません。
- 道具・許可名義・名義検査の例外一覧は、デフォルト枝の版だけを読みます。枝側で例外一覧を広げても通りません。枝で足した試験データが枝側の例外一覧に頼っているときは、その例外が人のレビューを経てデフォルト枝に入るまで赤のままです。
- `identity_gate.py` はデフォルト枝に無い commit を見ます。デフォルト枝の commit は、その push の CI が当時の版で見ています。ラチェットと gitleaks はその ref の全履歴を見ます。基準値はデフォルト枝の版より緩められません。
- 注釈付きタグは、commit に直す前にタグの作者（tagger）と注釈文を見ます（入れ子のタグもたどります）。中身は pre-push と同じ `identity_gate.check_pushed_tags` で、許可名義はデフォルト枝の版です。commit がきれいでも、タグの作者や注釈文が名簿に無ければ赤です。
- 権限は `contents: read` だけで、Issue やコメントは作りません。ログに出すのは件数と種類（例：`赤: 検査 2/5（ref 1 件）: gitleaks: gitleaks github-pat 1 件`）だけです。ref 名・ファイル名・作者名（commit の作者・タグの作者）・注釈文・値は個人名が入りうるので出さず、commit やタグのハッシュも出しません。git を含む各検査の stdout・stderr は作業場のファイルにだけ書きます。`actions/checkout` は取った枝の名前と完全な SHA（全履歴なら取った ref 名も）をログに出すので使わず、全部の枝・タグ・PR の head と検査の版（この実行のデフォルト枝の commit）を `git fetch -q` と静かな checkout で取ります。デフォルト枝の名前と SHA もログの本文には出しません（実行の画面に GitHub 自身が出すこの実行の commit は別です）。ref ごとの checkout は、`.gitattributes` の filter（LFS を含む）を切り、シンボリックリンクを普通のファイルとして置く専用の作業場で行います。git が何か出力した・失敗した ref（長すぎるファイル名、working-tree-encoding に変換できない中身、git が拒むパスなど）は `checkout できない ref` として赤にし、残りの ref の検査は続けます。LFS のポインタは中身を検査できないので、その件数を赤として出します。どの ref が赤かは、手元で同じ検査を回して確かめます。手動実行はデフォルト枝の版でだけ動きます。

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

数える検査は `clean_worktree`、`required_documents`、`secret_scan`、`personal_path_scan`、`commit_identity`、`dependency_configuration`、`ci_configuration`、`origin`、`repository_consistency` です。件数欄が無い検査は `fail` か `unknown` を1件と数えます。

公開名義の検査`public_attribution`は、この件数比較とは独立した必須検査です。共通CIと`preflight_ratchet.py`は既定で`references/public-commit-identities.json`を使い、全履歴の作者・コミッター・共同作者・署名行を検査します。`REDACTED`等の伏せ字や許可外の名義は、基準値を増やしても免除できず`blocked`（終了コード1）になります。結果が欠ける・検査不能なら終了コード2で止まります。名義やメールの生値は結果に出しません。既定の一覧はこのprojectの公開名義です。別の名義で使う導入先は、レビューした一覧を`--allowed-identities`で指定してください。

非公開設定を使う`identity_gate`はCIの件数比較には含めません。これは公開名義の検査で個人情報照合全体を代替できるという意味ではありません。手元の公開・共有前検査では、非公開設定を使う両gateを通す必要があります。

## 4. AI エージェントの直接公開を止めるフック

公開コマンドを使う運用を、Claude Code が操作する手前で機械的に守らせます。`scripts/publish_guard_hook.py` は PreToolUse フックで、次の操作を止めて公開コマンドへ案内します。

- `gh repo edit` での公開への切り替え、`gh repo create` での公開 repository の作成
- `gh api` や `curl` から GitHub API へ非公開の解除を送る操作
- GitHub MCP の `create_repository` で、非公開を明示していない作成（既定は公開）

公開コマンドは内部で `gh` を呼ぶため、このフックからは見えずにそのまま通ります。標準ライブラリだけで書いてあり、入出力は UTF-8 で扱うので、macOS・Linux・Windows で同じように動きます（CI で3つの OS のテストを通しています）。

repo-preflight の clone で次の1行を実行すると、ユーザー設定（`~/.claude/settings.json`、Windows は `%USERPROFILE%\.claude\settings.json`）に足します。すべての repository とセッションに効きます。既存の設定は残し、元のファイルは `settings.json.bak` に保存します。同じ登録があれば何もしません。clone を移した後などで登録が古い場所を指していれば、今の場所に書き直します。スキルの置き場所のようなシンボリックリンク経由で実行した場合は、リンク先ではなくリンクのパスで登録するので、clone を移してもリンクを張り直すだけで済みます。

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

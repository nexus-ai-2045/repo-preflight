# Gitフックの導入と負荷

`repo-preflight`は、検査に成功した後で既存のフックを呼ぶ管理CLIを提供します。他プロジェクトのスキルは必要ありません。cloneや検査だけではフックを有効にしません。

## 対象と導入前の確認

管理CLIはmacOS・LinuxなどのPOSIX環境向けです。Windowsでは変更前に`unsupported_platform`で停止します。既存のシェルテンプレートと、新しい管理CLIの対応範囲を区別してください。

```bash
python3 scripts/manage_git_hooks.py plan --repo /path/to/target
python3 scripts/manage_git_hooks.py status --repo /path/to/target
```

`plan`と`status`は読み取りのみです。`--repo`には対象リポジトリのルートを指定します。対象リポジトリに入る変更を確認し、その対象への導入を承認してから実行します。

```bash
python3 scripts/manage_git_hooks.py install --repo /path/to/target
```

導入時に使ったPython 3.9以降を固定します。固定版のgitleaksをPATHに置く必要があります。検査器やPythonが変わると、フックは停止し、更新による再固定が必要になります。

## 保持する状態

- 既存の`core.hooksPath`は既定で衝突として拒否します。未設定の場合、または下記の明示したglobal継承の場合に限り、管理ディレクトリを対象リポジトリのローカル設定へ登録します。既存のglobal設定は上書きしません。local・worktreeの別の設定は自動統合しません。
- Gitのcommon directory内に管理ディレクトリを置き、linked worktreeでも同じ管理状態を共有します。
- 既存フックは元の場所・内容・モードのまま残し、共通検査に成功してから元のパスで呼びます。引数と`pre-push`の標準入力、既存の`.githooks`への継承も保持します。他の種類のフックを追加・削除した場合は状態を確認し、更新してください。
- 管理ファイルや登録が変更されていたら、更新や解除で上書きせず停止します。Gitの環境変数で別のリポジトリを指定されても、管理操作は`--repo`の対象に束縛します。
- 同時操作と操作途中の状態を検出します。途中状態を正常な導入済みとは報告しません。

Gitがフックに渡す引数・標準入力と実行場所は[Gitの公式仕様](https://git-scm.com/docs/githooks)に従います。ローカルフックを回避できる権限や直接のGitHub操作までは制限しないため、CIの必須検査は引き続き必要です。

## 更新と解除

単一のglobalフックをそのまま呼び継ぐ場合は、内容と対象を確認してから明示します。

```bash
python3 scripts/manage_git_hooks.py plan --repo /path/to/target --chain-global-hooks --gitleaks /path/to/pinned/gitleaks
python3 scripts/manage_git_hooks.py install --repo /path/to/target --chain-global-hooks --gitleaks /path/to/pinned/gitleaks
```

global側のファイルは移動・変更・複製せず、元の場所で呼びます。引数・標準入力・失敗を引き継ぎ、登録元や実行ファイルの変化は検出して停止します。解除すると元のglobal設定が有効になります。他のリポジトリの設定は変えません。

`--gitleaks`は同梱の固定ハッシュに一致する実ファイルだけを受け付けます。導入後は記録したバイナリだけを専用環境変数で検査処理へ渡し、通常のPATHは変更しません。一般のPATHに別の版があっても混同せず、同じフォルダの別プログラムを優先することもありません。更新・解除に引数を再指定する必要はありません。状態確認は登録済みのファイルを検査し、その消失・変更を別の指定で隠しません。

```bash
python3 scripts/manage_git_hooks.py update --repo /path/to/target
python3 scripts/manage_git_hooks.py uninstall --repo /path/to/target
```

`update`は、移動や更新後のこのcheckoutから実行します。既存の管理フックと登録が一致していれば、検査器とPythonを再固定できます。`uninstall`は自分が登録したローカル設定と管理ファイルを取り除き、元の標準フックを使う状態へ戻します。元のフック自体は変更しません。

起動用の小さなコードを導入時に管理ディレクトリへ固定し、checkoutのコードは照合後に呼びます。これは、ローカルファイルを自由に書き換えられる利用者に対する署名や権限制御ではありません。

## AIの入口から使う

```bash
python /path/to/skill/run_preflight.py --git-hooks plan --repo /path/to/target
python /path/to/skill/run_preflight.py --git-hooks status --repo /path/to/target
```

Claude Codeと共有agentsのスキルは同じcheckoutを呼びます。CodexやGeminiの入口が共有agentsへのリンクなら、そのリンクを保って同じ版へ更新します。スキルの配置、CLIの実行、AIが指示を読み込むことは別です。既存セッションが内容を保持する環境では、再読み込みか新しいセッションで採用を確認してください。

新規リポジトリの作成手段をこの入口に縛らず、コメント禁止lintの自動導入も行いません。GitHub APIや画面など、ローカルGitを通らない操作までGitフックが拒否するものではありません。

## 操作ごとの検査

| 実行場所 | 対象 |
|---|---|
| `pre-commit` | 作者・コミッター、ステージした差分とファイル名、固定版gitleaks |
| `commit-msg` | コミットメッセージの名義・署名行・秘密情報などの形 |
| `pre-push` | 送信するコミットとタグ、固定版gitleaks、送り先に対する基準値の緩和 |
| イベント時のCI | イベントの履歴・比較元・PR本文などの検査 |
| 全ref監査 | 取得した枝・タグ・PR先端の履歴。削除済みの内容も対象 |

全履歴を調べる処理を3つのローカルフックへ追加しません。初回pushなどで送信範囲自体が大きい場合は、`pre-push`の処理も増えます。

## 再計測

```bash
python3 scripts/benchmark_git_hooks.py --mode managed --samples 5 --commits 20
python3 scripts/benchmark_git_hooks.py --mode managed --compare-root /path/to/before --samples 5 --commits 20
```

固定した検査器とgitleaksを使い、一時リポジトリで実際のフックコマンドを実行します。導入と解除は計測区間の外です。比較時は実行順を交互に変え、終了コード・検査器のハッシュ・各回の秒数・中央値・範囲をJSONで返します。測定中に検査器が変わった場合は失敗します。

この合成データでの測定は、巨大な差分・媒体ファイル・ネットワーク送信やGitHubのジョブ全体の所要時間を保証しません。

## CIの重複削減

コミット本文は`git cat-file --batch`で読み、コミットごとのGit起動を減らします。サイズ区切り・OID・型・終端・UTF-8を検査し、不正な応答や読取り失敗を成功へ変えません。既存のGit呼出しを注入する経路は、その契約を保ちます。

全ref監査のLFSポインタ検査は、列挙済みの先端の和集合からblobを一度ずつ調べます。各refの比較基準・checkout・名義・タグ注釈・gitleaks検査は維持します。検査結果のキャッシュによる省略や、全履歴検査の差分検査への置換は行いません。

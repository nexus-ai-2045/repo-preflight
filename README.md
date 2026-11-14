# Repo Preflight

Gitリポジトリを共有する前に、秘密情報らしき文字列・個人のパス・文書の不足や食い違いを調べるCLIです。人・AIエージェント・CIから使えます。

## 目的

削除済みの内容を含む、取得済みのGit全履歴を検査し、共有前に直すべき点を見つけます。
検査成功は、公開・push・マージの承認でも、秘密情報や個人情報が無いことの保証でもありません。

## できること

- APIキーなどの既知の形式と個人パスを、履歴・ファイル・コミット情報から検出します。
- **必須の文書**（README.md、LICENSE、SECURITY.md、CONTRIBUTING.md、PREFLIGHT.md）と、文書・コマンド・コードの整合性を確認します。
- 作者・コミッター・共同作者行を、非公開の名義設定やレビュー済みの公開許可一覧と照合します。
- `--intent`で操作前の確認事項をJSONとして返します。GitHub Settingsは実測と変更案の提示まで行います。

現在版は **0.14.0** です。今回の変更と移行事項は[リリースノート](docs/releases/v0.14.0.md)、過去の変更は[CHANGELOG](CHANGELOG.md)を参照してください。

## 使い方

| やりたいこと | 最初に読むところ |
|---|---|
| 手元のリポジトリを検査する | 以下の「最短の検査」 |
| AIエージェントの操作前に確認する | 以下の「非公開設定とAI連携」 |
| GitHubのマージ前に検査する | [共通CIの導入手順](docs/publish-and-ci-gate.md#2-共通-ci-ゲート) |
| commit・push時に自動で止める | [Gitフックの導入・確認・更新・解除](docs/git-hooks.md) |

### 最短の検査

テキストと標準PNGの検査は、Python 3.9以降とGitで動き、追加パッケージは不要です。

```bash
git clone https://github.com/nexus-ai-2045/repo-preflight.git
cd repo-preflight
python scripts/readiness_scan.py --repo /path/to/your-repo
```

対象は別の場所でも構いません。通常の検査は対象を読み取るだけです。
整合性検査に比較元が必要なら、未指定時は利用可能な`origin/main`を試します。

JPEG・GIF・PSD・CMO3を含む対象には、画像用の追加依存が必要です。
Python 3.11以降の仮想環境で導入し、同じPythonから検査してください。

```bash
python -m venv .venv
.venv/bin/python -m pip install ".[images]"
.venv/bin/python scripts/readiness_scan.py --repo /path/to/your-repo
```

Windowsでは`.venv/bin/python`を`.venv\Scripts\python`に読み替えます。
GIF・PSDなどのPillow使用経路とPDF画像検査は、Python 3.11以上かつPillow 13（`>=13,<14`）が必要です。
旧版・不明版・欠損は、入力をデコードする前に検査不能として停止します。
Python 3.9・3.10のテキストと標準PNG検査は維持します。旧Pillow 11互換は提供しません。
[実行環境と形式ごとの制約](docs/runtime-support.md)、[下限変更と移行](docs/adr/0019-pillow-media-runtime-floor.md)を確認してください。

### 非公開設定とAI連携

名義検査は必須です。最初の検査前に、対象の外で管理する非公開設定を用意してください。
値や設定ファイルを公開リポジトリ・PR・チャットへ貼らないでください。

| 設定 | 用途 |
|---|---|
| `PREFLIGHT_PRIVATE_PATTERNS_FILE` または `PREFLIGHT_PRIVATE_PATTERNS` | 名義・共同作者行を照合するJSON |
| `PUBLIC_IDENTITY_DENYLIST` | 操作intentで必須の、個人識別子リストの絶対パス |

設定の形式・適用範囲は[CLI・AI連携の詳細](docs/cli-reference.md)にあります。
持ち主の名義を環境から集め、値を表示せず照合する手順は[ローカル名義ガード](docs/local-identity-guard.md)を参照してください。

```bash
python scripts/readiness_scan.py --repo /path/to/your-repo \
  --intent open_pr --base-ref origin/main --human
```

AI用skillの導入は[ランタイム対応](docs/runtime-support.md)、操作前の契約は[SKILL.md](SKILL.md)を参照してください。
呼出側は未設定・未確認・失敗なら外部操作を中止します。skillの配置だけでは操作を止めません。
全intent・対話の状態・PRレビューの確認方法は[CLI・AI連携の詳細](docs/cli-reference.md)にまとめています。

### 結果の読み方

| 検査結果 | 終了コード | 次の対応 |
|---|---|---|
| `pass` | 0 | 検査範囲と内容を人が確認する |
| `blocked` | 1 | 指摘を直して再検査する |
| `tool_error` | 2 | 検査できていない原因を直す |

これは検査モードの結果です。`--intent`の対話と抑止記録には[別の状態語彙](docs/cli-reference.md#結果の読み方)があります。
検出種別と安全な修正箇所はJSON・フック・人向けレポートに返します。
ファイル・行・コミット・名義欄や、位置を特定できない場合の読み方は[診断ガイド](docs/diagnostics.md)を参照してください。

## 制約

- 既知の形式による検出です。個人情報の最終判断、権利確認、依存ライブラリの脆弱性検査は行いません。
- 未取得の履歴、独自形式・圧縮ファイル内などは見逃し得ます。CMO3内部パス検査は現在の作業ツリーに限ります。
- 非公開送信の`--media-inspection metadata`は、原本SHA-256とGit objectを照合してメタデータを調べます。映像・画像の内容に個人情報が無いことは保証しません。公開・releaseには適用しません。
- 通常の検査はpush・PR作成・マージやSettings変更を実行しません。検査成功はGitHub CI成功や保護設定の有効化の証明ではありません。

自動で止まるのは接続した操作経路です。cloneだけでは通常の`git push`やGitHub画面の操作を止めません。
ローカルでは検査失敗時に中止する入口、GitHubでは共通CIと対象branchの必須チェック設定が必要です。
公開範囲の変更は人の承認後に`publish_gate.py`を経由します。直接操作・別branch・回避権限まで一律に制限しません。

共通CIのラチェットは指摘の新規増加を止めます。既存の基準値があれば指摘0件を意味しません。許可外の公開名義は基準値で免除しません。
接続と実行結果は[公開コマンドとCI・保護設定](docs/publish-and-ci-gate.md)、保証の境界は[保証すること・しないこと](docs/guarantees-and-limits.md)を参照してください。

## 開発・検証と資料

CIの互換性検査は、LinuxのPython 3.11・3.13、WindowsのPython 3.13、macOSのPython 3.9の4環境です。
Python 3.9は検査の試験のみで、開発用の整形と`ai-ratchet-gate`は3.11以上です。
開発・テスト・CIには`pip install -e ".[test]"`が必要です。[開発手順](CONTRIBUTING.md)を参照してください。

```bash
python scripts/runtime_smoke.py --repo .
```

| 調べたいこと | 参照先 |
|---|---|
| 全引数・非公開設定・メディア検査 | [CLI・AI連携の詳細](docs/cli-reference.md) |
| 関連テストの選択・手元とCIの責務・リリース | [運用手順](OPERATIONS.md) |
| 文書とコードのずれ・workflowの責務 | [整合性ゲート](docs/repository-consistency-gate.md) |
| 登録済み個人情報のCI導入・必須チェック化 | [導入手順](docs/private-identity-ci.md) |
| Settingsの実測・README設計の検査 | [設定ガイド](references/github-settings.md)・[README検査実装](scripts/readme_release_gate.py) |
| AI憲法の入口・設計判断 | [入口契約](docs/ai-constitution-entry-contract.md)・[ADR一覧](docs/adr/README.md) |

脆弱性報告は[SECURITY.md](SECURITY.md)、ライセンスは[MIT](LICENSE)です。

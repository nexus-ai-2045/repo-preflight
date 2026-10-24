# Repo Preflight

**Gitリポジトリを共有する前に、秘密情報らしき文字列・個人のパス・文書の不足や食い違いを調べるCLIです。** 人・AIエージェント・CIから使えます。

## 目的

検査成功は、公開・push・マージの承認ではありません。個人情報や秘密情報が絶対に無いことも保証しません。

## できること

- APIキーなどの既知の形式と個人パスを、取得済みのGit全履歴（削除済みを含む）から検査します。形式やscopeによる限界があります。
- **必須の文書**（README.md、LICENSE、SECURITY.md、CONTRIBUTING.md、PREFLIGHT.md）と、文書・コマンド・コードの整合性を確認します。
- コミットの作者・コミッター・共同作者行を、指定した名義設定や公開許可一覧と照合します。
- `--intent`で、操作前の確認事項をJSONとして返します。

## 使い方

検査だけなら **Python 3.9以降とGit**（Mac 標準の 3.9 も可） で動きます。text と PNG だけなら追加パッケージは不要です。JPEG・GIF・PSD・CMO3 を含む repository は、下の `.[images]` が必要です。画像用追加依存が無い経路は検査不能で止まります。

```bash
git clone https://github.com/nexus-ai-2045/repo-preflight.git
cd repo-preflight
python -m venv .venv
.venv/bin/python -m pip install ".[images]"
.venv/bin/python scripts/readiness_scan.py --repo /path/to/your-repo
```

調べるリポジトリは別の場所でも構いません。通常の検査は対象を読み取るだけです。整合性検査に比較元が必要な場合は、未指定なら利用可能な`origin/main`を試します。

Windowsでは`.venv/bin/python`を`.venv\Scripts\python`に読み替えます。

**開発・テスト・CI**では `pip install -e ".[test]"` が必要です。手順は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

| 結果 | 終了コード | 次の対応 |
|---|---|---|
| `pass` | 0 | 検査範囲と内容を人が確認する |
| `blocked` | 1 | 指摘を直して再検査する |
| `tool_error` | 2 | 検査できていない原因を直す |

**GitHubのCIだけで使う場合、手元へのcloneは不要です。** [共通CIの導入手順](docs/publish-and-ci-gate.md#2-共通-ci-ゲート)を参照してください。

## 自動で止めるには

cloneやskill配置だけでは、通常の`git push`やGitHub画面の操作は止まりません。

| 使う場所 | 必要な接続 |
|---|---|
| ローカル・AI | 検査を呼び、失敗したら次の操作を中止する入口を使う |
| GitHubのマージ | 共通CIを導入し、そのチェックを対象branchで必須にする |
| 公開範囲の変更 | 人の承認後、専用の`publish_gate.py`を経由する |

持ち主の個人名義を環境から集めて照合する `scripts/local_identity_guard.py` と、名義・共同作者行の検査も含みます。名義検査は全体の必須判定になり、非公開設定が無ければ止まります。詳しい変更と保証範囲は[変更履歴](CHANGELOG.md)、PNG・JPEGの検査は[実行環境の説明](docs/runtime-support.md#pngを含むprivate-identity検査)を参照してください。

止まるのは接続した経路と保護対象の操作です。直接操作、別branch、回避権限まで一律に制限しません。[公開コマンドとCI・保護設定](docs/publish-and-ci-gate.md)で接続と実行結果を確認してください。

共通CIのラチェットは指摘の**新規増加**を止めます。既存の指摘を許容する基準値がある場合、指摘0件の意味にはなりません。許可外の公開名義は基準値で免除しません。

結果の語彙、必須文書、保証の境界は [CLI・AI連携の詳細](docs/cli-reference.md) と [保証すること / 保証しないこと](docs/guarantees-and-limits.md) にあります。PNG・JPEGの検査は [実行環境の説明](docs/runtime-support.md#pngを含むprivate-identity検査) を参照してください。

## AIエージェントから使う

AI用skillの導入手順は[ランタイム対応](docs/runtime-support.md)、操作前の契約は[SKILL.md](SKILL.md)です。

```bash
python scripts/readiness_scan.py --repo /path/to/your-repo \
  --intent open_pr --base-ref origin/main --human
```

操作intentでは非公開の名義設定・個人識別子リストが必要です。未設定・未確認・失敗なら、呼出側が外部操作を中止する必要があります。これらの設定を公開リポジトリへ置かないでください。

対話の状態・全intent・非公開設定・PRレビュー確認は[CLI・AI連携の詳細](docs/cli-reference.md)にまとめています。skillを配置したことと、実際に呼ばれて操作を止めたことは別です。

## 制約

- 既知の形式による検出であり、個人情報の最終判断、権利確認、依存ライブラリの脆弱性検査は行いません。
- 取得していない履歴、独自形式・圧縮ファイル内などは見逃し得ます。`.cmo3`の内部パス検査も現在の作業ツリーに限られます。
- Settingsの実測と差分提示はできますが、設定変更は行いません。通常の検査はpush・PR作成・マージを実行しません。
- ローカル検査の成功は、GitHub CI成功・branch保護の有効化・全操作経路での拒否の証明にはなりません。

範囲と例外は[保証すること・しないこと](docs/guarantees-and-limits.md)を参照し、人のレビューや専門の検査と併用してください。

## 詳しい手順

| 調べたいこと | 参照先 |
|---|---|
| 全引数・結果・名義設定・PRレビュー | [CLI・AI連携の詳細](docs/cli-reference.md) |
| CI・公開ゲート・必須チェック | [導入ガイド](docs/publish-and-ci-gate.md) |
| 文書とコードのずれ | [整合性ゲート](docs/repository-consistency-gate.md)・[検査実装](scripts/consistency_gate.py) |
| GitHub Settingsの実測 | [設定ガイド](references/github-settings.md)・[検査実装](scripts/github_settings_gate.py) |
| README設計の検査 | [検査実装](scripts/readme_release_gate.py) |
| AI憲法の入口 | [入口契約](docs/ai-constitution-entry-contract.md) |
| 変更点・更新 | [CHANGELOG](CHANGELOG.md)・[v0.9.0](docs/releases/v0.9.0.md) |
| 開発・設計判断 | [CONTRIBUTING](CONTRIBUTING.md)・[ADR一覧](docs/adr/README.md) |

CIの対象はLinux/macOSがPython 3.9・3.11・3.13、WindowsがPython 3.13です（3.9 は検査の試験だけで、開発用の整形と ai-ratchet-gate は 3.11 以上）。ランタイムの検査は次で実行できます。

```bash
python scripts/runtime_smoke.py --repo .
```

脆弱性報告は[SECURITY.md](SECURITY.md)、ライセンスは[MIT](LICENSE)です。

登録済み個人情報の CI 接続・必須チェック化・検査器の更新は、[導入手順](docs/private-identity-ci.md)を参照してください。導入前の候補と運用済みを区別します。

PNGの対応範囲と検査不能時の扱いは、[実行環境の説明](docs/runtime-support.md#pngを含むprivate-identity検査)を参照してください。

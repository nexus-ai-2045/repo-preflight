# Repo Preflight

Repo Preflightは、Gitリポジトリを**見せる相手を広げてよいか**判断するための、読み取り専用CLIとチェック手順です。ローカルのファイルとGit履歴を調べ、機械で確認できたこと、人が確認すべきこと、確認できなかったことをJSONで分けて返します。

public化専用ではありません。private repositoryをチームへ開くとき、成果物を客先へ納品するとき、外部の協力者へ渡すときも、必要な検査は同じです。public化は数ある到達点のひとつとして扱います。

## 目的

テスト成功と公開許可を混同しないことが目的です。文書、Git状態、秘密情報らしい文字列、個人環境の絶対パス、依存定義、CI設定を自動検査し、実際のCI結果、依存関係の最新脆弱性、人間の目視確認は別の証拠として残します。

## できること

- repo root、Git状態、remote、作者履歴を検査
- README、LICENSE、SECURITY、CONTRIBUTING、PREFLIGHTを確認
- 現在treeと全Git履歴のsecret・個人path候補を検査
- 依存定義ファイルの有無とCI設定の最低限の構造を確認
- 自動検査結果と、人間レビューを含む公開判断を分離
- 組織固有のプロジェクト一覧、アカウント名義、通知先などをCLI本体から分離

## クイックスタート

```powershell
python scripts/readiness_scan.py --repo C:\path\to\repo --json
```

release準備ではREADME設計ゲートも自動実行します。

```powershell
python scripts/readiness_scan.py --repo C:\path\to\repo --release --json
```

READMEは、短い導入、目的、できること、Quickstart、制約、見出し階層、全体の長さを
read-onlyで検査します。不足に応じて Template Creator、Product Design、Visualize、
Figma / frontend-design、ai-slop-check のうち必要なものだけを提案します。

終了コード:

- `0`: scannerが扱う必須項目がpass
- `1`: failまたはunknownがあり`blocked`
- `2`: Gitや履歴取得など検査自体が失敗

scannerは既定でread-onlyです。GitHub repo作成、push、PR、merge、visibility変更、投稿は行いません。
`--release` もREADMEやreleaseを自動変更・作成しません。

JSONの`status: pass`は、このCLIが担当するローカル自動検査に合格したという意味です。`publication_decision`は常に人間レビューを要求します。`pass`だけを根拠に公開しないでください。

## 判定の限界

内蔵チェックは、ローカルrepositoryで確認できる基本項目を対象にします。次の項目はrepositoryごとに追加確認が必要です。

- 使用ライブラリに既知の脆弱性がないか
- 第三者のコード、文章、画像などを公開する権利があるか
- GitHubでbranch保護やreview必須設定が有効か
- GitHub ActionsなどのCIが実際に成功したか
- CI失敗や障害を担当者へ通知できるか
- 問題発生時に以前の安全な版へ戻す手順を実際に試したか
- README、個人情報、公開範囲を人が目視確認したか

内蔵の正規表現は代表的な秘密情報の形式を検出する補助機能です。秘密情報が存在しないことは保証しません。独自形式、分割・符号化された値、2 MBを超える履歴ファイル、画像やバイナリ内の情報などを見逃す可能性があります。対象リポジトリ固有の検査、専門のsecret scanner、依存関係の監査、人間レビューを必ず併用してください。

## Skill

Codexなどから使う場合は [SKILL.md](SKILL.md) を入口にしてください。状態遷移、承認境界、成果物テンプレートを同梱しています。

公開repositoryのruleset、merge方式、Actions権限、security機能などの選択理由は、[GitHub repository設定ガイド](references/github-settings.md)を参照してください。

## License

MIT License。詳細は [LICENSE](LICENSE) を参照してください。

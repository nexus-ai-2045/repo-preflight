# 公開対象と確認の入口

## 公開するもの

公開対象は、このリポジトリの汎用CLI、テスト、説明文書、テンプレート、AI用skill、および検査用の依存wheelです。GitHubでは、ファイルに加えてコミット履歴、PR、コメントも閲覧可能になります。

## 含めないもの

個人識別子を照合する非公開設定、credential、個別案件の原資料、私用端末の設定、非公開の実行記録は公開対象に含めません。公開前には、現在のファイルと削除済みを含む全履歴を既存の検査CLIで照合します。

## 検査とライセンス

- 公開操作は [publish_gate.py](scripts/publish_gate.py) のdry-runと人間の確認を経て実行します。
- [共通CIの呼出し](.github/workflows/repository-preflight.yml)から既存の検査を実行し、[基準値](.repo-preflight/ratchet-baseline.json)は全項目0件から増やしません。
- CLIの利用方法と保証範囲は [README](README.md)、公開範囲の方針は [PREFLIGHT](PREFLIGHT.md)を参照してください。
- 本体は [MIT License](LICENSE)です。同梱wheelの原本・ライセンス・ハッシュは [vendor](vendor/README.md)を参照してください。
- 脆弱性の報告経路は [SECURITY](SECURITY.md)を参照してください。

## この文書では保証しないこと

この文書の存在や機械検査の合格は、公開承認やGitHub CIの成功を意味しません。CIの実行状況はGitHub Actionsで確認します。workflowの配置だけで、GitHubの必須チェックやbranch保護が有効になるわけではありません。

検査したcommit、実行結果、未確認事項、人間の承認は、公開操作の担当者が非公開の運用記録に保存します。公開範囲の変更とリリースは別々に判断します。

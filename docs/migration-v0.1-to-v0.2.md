# v0.1.x から v0.2.0 への移行

v0.2.0 で2つの名前が変わりました。旧名 `public-readiness` は「public化がゴール」と読めますが、
この道具は private 保存やチーム共有も正規の完了地点として扱うため、設計と名前が食い違っていました。

| 対象 | v0.1.x | v0.2.0 |
|---|---|---|
| リポジトリ / パッケージ | `public-readiness` | `repo-preflight` |
| 必須のレビュー記録 | `PUBLIC_READY.md` | `PREFLIGHT.md` |
| README gate の schema | `public-readiness.readme-release-gate/v1` | `repo-preflight.readme-release-gate/v1` |

検査対象のリポジトリ側で必要な作業:

```bash
git mv PUBLIC_READY.md PREFLIGHT.md
```

さらに `PREFLIGHT.md` の先頭に次の1行を置いてください。

```markdown
<!-- repo-preflight:review-record -->
```

`PREFLIGHT` は一般的な語なので、deployment preflight 手順書のような無関係な同名ファイルが
存在しえます。CLIはこのマーカーの有無でレビュー記録かどうかを判別します。マーカーが無い場合は
`required_documents` が `invalid` として fail します。テンプレートは [assets/PREFLIGHT.template.md](../assets/PREFLIGHT.template.md)。

旧URL `github.com/nexus-ai-2045/public-readiness` はGitHubのリダイレクトで引き続き解決します。
旧名は永久欠番とし、再利用しません。

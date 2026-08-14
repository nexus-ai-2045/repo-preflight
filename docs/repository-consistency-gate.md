# リポジトリ整合性ゲート

PR（プルリクエスト）をマージする前に、README・docs・実装・テスト・設定の関係を静的に検査する共通ゲートです。repo 固有側には `.repo-preflight-consistency.json` だけを置き、検査ロジックを各 repo の `scripts/preflight.py` へ複製しません。

## 検査内容

- Markdown の相対リンクが repo 内の実在先を指すか
- README に宣言したコマンド文字列があり、そのコマンドが参照する file が存在するか
- `impact_map` で指定した実装・設定変更に対し、関連 docs または tests が同じ差分に含まれるか
- SSOT（単一の正本）に指定した file が変わった時に生成物も更新されたか、および生成物が宣言済み SHA-256 と一致するか

README の自由文からコマンドを推測すると誤検知が増えるため、契約対象だけを `readme_contracts` に宣言します。外部 URL はネットワーク状態に依存するため検査しません。任意コマンドも実行せず、repo 設定からのコード実行を防ぎます。

## 導入手順

1. `assets/.repo-preflight-consistency.template.json` を対象 repo の `.repo-preflight-consistency.json` へコピーし、影響マップを repo の構造に合わせます。
2. 最初は必ず `"mode": "shadow"` にします。所見は `checks.repository_consistency` に出ますが、既存の pass / blocked は変更しません。
3. 通常の PR 差分を複数回測り、真陽性・誤検知・未検知を記録して影響マップを狭めます。
4. 人間レビュー後だけ `"mode": "enforce"` へ変更します。

`enforce` では所見が 1 件でも `status=blocked` になります。設定不正、Git 差分取得失敗、読めない Markdown は `tool_error` とし、成功扱いにしません。

## GitHub Actions 接続例

```yaml
- uses: actions/checkout@v5
  with:
    fetch-depth: 0
- run: python scripts/readiness_scan.py --repo . --intent open_pr --base-ref refs/remotes/origin/${{ github.base_ref }}
```

push、PR 作成、merge、公開はそれぞれ別の承認境界です。このゲートのローカル成功だけで、いずれも自動承認されません。

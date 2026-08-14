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
4. 既知の所見を `ratchet.baseline` に固定して `"mode": "ratchet"` にします。新規所見と、改善済みなのに残された baseline の両方を block します。改善済み項目を baseline から削除すると、同じ問題の再発は新規悪化として止まります。
5. baseline が空になり、人間レビュー済みになった後だけ `"mode": "enforce"` へ変更します。

`enforce` では所見が 1 件でも `status=blocked` になります。設定不正、Git 差分取得失敗、読めない Markdown は `tool_error` とし、成功扱いにしません。

設定は同梱 JSON Schema と同じキー・型を runtime でも検証します。未知キー、未知の入れ子キー、文字列配列内の `bool` / 数値、重複した ratchet baseline は `invalid_consistency_config` です。

Git inventory は index mode を保持し、symlink (`120000`) と gitlink (`160000`) を検査対象から除外します。Markdown symlinkや、解決後にrepo外へ出るpathの内容は読み取りません。

## 可読性と専門 capability の推薦

変更 file に応じて `capability_recommendations` を出します。README・docs では Template Creator と Product Design、security・auth では Security Guidance、画像・動画では Creative Production、OpenAI API/Appでは OpenAI Developers、`.github/` では GitHub を候補にします。

推薦は所見の根拠 path と一緒に表示し、常に `human_review_required` です。repo-preflight 自身が plugin を実行したり、外部送信・画像生成・GitHub変更を行ったりはしません。該当変更がない plugin は推薦しません。

## GitHub Actions 接続例

```yaml
- uses: actions/checkout@v5
  with:
    fetch-depth: 0
- run: python scripts/readiness_scan.py --repo . --intent open_pr --base-ref refs/remotes/origin/${{ github.base_ref }}
```

push、PR 作成、merge、公開はそれぞれ別の承認境界です。このゲートのローカル成功だけで、いずれも自動承認されません。

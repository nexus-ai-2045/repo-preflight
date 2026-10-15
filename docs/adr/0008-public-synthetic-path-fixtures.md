# ADR-0008: 公開の合成path fixtureを完全束縛した明示policyで扱う

- status: Proposed
- date: 2026-10-16

## 背景と判断

v0.6.0の全履歴検査は、説明用の `My Name` とエラー秘匿テストの `me` に由来する5つの古いblobで停止した。現行treeからはすでに除去済みだが、履歴の検査は維持する。公開履歴の書換え、全体除外、正規表現の緩和、検査範囲の縮小は行わない。

ADR-0005/0006の明示指定JSONと完全hash束縛を再利用し、`version: 3` を追加する。公開fixture用であり、private原資料を公開可能と判断する入口ではない。v1のsecret例外、v2のprivate限定path例外は変更しない。

## 機械的な制限

- policyは自動読込せず、`--reviewed-secret-exceptions` の明示指定だけで使う。
- version 3はpath rule専用。secret ruleを登録できない。
- 検出値hashはscanner内の有限な予約値集合に限定する。今回必要な説明用WindowsのMy、その中で重複検出されるmacOS形式のMy、Linuxのmeだけを対象とする。任意のユーザー名をpolicyで追加できない。
- 予約値も自動除外しない。repo、相対path、生byte全体のSHA-256、rule、検出値SHA-256、件数がすべて一致した検出だけに適用する。
- UTC期限とレビュー参照を必須とし、期限は将来かつ90日以内とする。
- GitHub APIでexact originのfull_name、private=false、visibility=publicをlive確認する。private/internal/unknown/offline/型不正は停止する。
- 同じblobの別path、内容・件数の変更、追加の個人path、secret、UTF-16やURL復号などの未審査検出は止める。途中commitと削除済みfileも検査する。
- 結果には例外IDと適用件数だけを出し、生の検出値は表示しない。合成値の意味と個人情報を含まないことはレビューの責任とする。

## レビュー記録の配置

今回の5履歴blob、11entryの内容hash・検出hash・件数・期限は、利用者側の非公開運用記録へ保存する。公開coreには個別repoの例外policyや実行記録を含めない。`PREFLIGHT.md` の配置契約を維持する。

## 利用と検証

```sh
python scripts/readiness_scan.py --repo . --intent release --consistency-base-ref v0.5.1 --reviewed-secret-exceptions "reviewed-public-fixtures.json"
```

通常scanでは引き続き5候補を検出する。明示policy付きscanだけが、完全一致した合成検出をレビュー済みとして報告する。policyの期限切れ時は停止し、内容・予約値・レビュー参照を再確認する。passはreleaseの承認ではない。

回帰検査は公開可視性のlive照合、未知のホーム名拒否、secret rule拒否、変更・追加・alias、期限・型・review参照、UTF-16・URL復号、削除済み履歴、target diffを含む。

# ADR-0005: レビュー済みsecret誤検知を完全束縛した例外で扱う

- status: Proposed
- date: 2026-10-01

## 背景と判断

旧識別子がsecret候補の部分文字列と一致する。検出regexに単語境界を加える案は、埋め込まれた本物のcredentialを見逃すため採用しない。識別子の改名や検査停止で回収を通さない。

任意CLI/API指定でのみ、対象repo、相対path、生byte全体hash、安定rule ID、検出文字列hash、検出件数、レビュー理由を指定するJSON例外を使う。全条件一致の検出だけを除外し、通常の全経路検出を維持する。履歴は同じblobの全到達pathを検査する。representation間は同じルール・検出hashの最大件数を採用し、同じ検出の復号前後の二重計上を避ける。

未知キー・重複・型不正・origin不一致は停止する。レポートは適用ID・件数だけを返し、例外の理由や検出内容を出さない。設定にcredentialの生値を保存しない。任意credentialの不存在は機械的に保証できないため設定自体も人間レビューを要求する。

## 承認境界

このADRは公開上流へのpush、PR、merge、runtime/hookへの導入の承認を与えない。ローカル差分・検証・独立レビューの後、それぞれ対象と操作を提示し別途確認する。例外を自動探索・自動生成しない。

## 使い方（運用詳細）

既知の誤検知を内容まで確認したときだけ、`--reviewed-secret-exceptions REVIEWED.json` を明示指定する。通常の secret 正規表現、UTF-8/UTF-16、URL復号、作業ツリーと削除済みを含む履歴の検査は維持する。例外ファイルは自動読込しない。`scan(..., reviewed_secret_exceptions=Path(...))` API、通常CLI、intent、interactiveで同じ指定を使う。

JSONの最上位は `version: 1` と `entries` 配列だけとする。各entryに以下を指定する。

| キー | 内容 |
|---|---|
| `id` | 英字で始まる英数字・`_`・`.`・`-`のみの識別子、最大64文字 |
| `repo` | originのGitHub `owner/name` と完全一致 |
| `path` | リポジトリ相対のPOSIXパス。絶対パス、逆斜線、`.`/`..`、空要素を禁止 |
| `content_sha256` | ファイルの生byte全体のSHA-256、小文字64桁 |
| `rule` | `openai_key` / `github_token` / `github_pat` / `aws_access_key` / `slack_token` / `private_key` |
| `match_sha256` | 正規表現が検出した文字列をUTF-8にしたSHA-256、小文字64桁 |
| `occurrence_count` | その内容内の検出件数、正整数 |
| `review_reason` | 非空の人間レビュー理由。認証情報を保存しない |

全束縛条件が一致した検出だけを除外する。同じ文字列のraw/URL復号/encoding間の重複はルールとhashごとの最大件数でまとめ、rawとURL表現の両方が存在する場合は復号後の合計件数を使う。別の検出文字列は残る。同一blobが複数pathで履歴に現れる場合は各pathを検査し、別pathの検出は除外しない。Gitパスの根拠がないblobは例外対象外とする。

不正な型、未知キー、JSON重複キー、重複ID/束縛、origin不一致は `tool_error` とする。サイズ上限は1MB、1000entryとする。内容・パス・件数が変われば検出を残す。結果の `checks.secret_scan.reviewed_exceptions` は適用IDと件数だけを返す。件数は作業ツリーと履歴の検査単位ごとの適用合計で、理由・path・検出文字列・digestは結果に出さない。

この指定は公開・push・導入の承認ではない。例外設定自体をレビュー対象にし、公開上流への反映と実行環境への導入は別途承認する。

## 検証と見直し

実Gitで作業ツリー・削除履歴・path alias・target diff・CLI/intent/interactiveを検証する。通常/埋込み/URL credentialが残ること、内容・件数・path変更、追加credential、不正設定で通過しないことを固定する。例外適用の証拠や履歴path列挙が不確実になった場合は例外拡大を止めて再設計する。

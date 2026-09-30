# ADR-0005: releaseの今回名義と既存公開履歴を分けて報告する

- Status: Proposed
- Date: 2026-10-01
- Decision owners: repository maintainers

## Context

`--intent release --expected-identity`は全refの既存コミットを単一名義で照合する。
既に公開済みのGitHub merge committer、bot、旧名義があると、今回release操作で
新たに送るコミットがなくても不一致になる。一方、公開名義のpush契約は
今回送るcommit rangeのauthor/committerとpush主体を検査する。
過去の公開履歴と今回操作を混同すると、履歴改変や不透明な例外へ誘導する。

## Decision

releaseに限り、`--expected-identity`と`--identity-base-ref origin/<branch>`を
明示したとき、指定remote refのOIDを固定し、HEADの祖先であることを検査する。
`commit_identity`の必須判定はそのOID以降のコミットと現在有効なGit名義に限る。
`historical_identity_audit`には全refの名義数と不一致件数を別に残す。
`--identity-base-ref`を省略した従来の全履歴必須判定は維持する。

secretと個人pathの全履歴・working tree検査、README更新、文書、clean検査は
狭めない。`publish`への適用はしない。GitHubの最新remote HEAD、pusher、
既存公開履歴の露出可否、tag/Releaseの実行承認は別証拠で確認する。
既存履歴に問題がある場合は報告し、人間判断なしに書き換えない。

## Alternatives considered

### GitHub committerを期待名義へ一律追加する

採用しない。botや旧名義もあり、単一例外では根因を解消しない。

### 既存履歴を書き換える

採用しない。既に公開済みのhash、署名、tag、参照先を変え、別の承認を要する。

### releaseで全履歴の検査を止める

採用しない。secretと個人pathの履歴検査契約を維持する。

## Consequences

- release結果から今回の名義判定と過去の名義分布を区別できる。
- 指定refがない、remote refでない、HEADの祖先でない場合はtool_errorになる。
- remote-tracking refの鮮度はローカルscan単体では証明できない。外部操作直前に
  GitHubの対象repo、branch、HEADを再取得し、packetのOIDと照合する。
- Hook配線、実Codexイベント、公開済み履歴の対処判断はこのADRの採用だけでは完了しない。

## Verification

- 既存履歴だけの名義不一致は監査件数に残り、今回コミットが一致すれば必須判定は通る。
- 今回コミットの名義不一致はblockする。
- 不正なrefやrelease以外の指定はtool_errorになる。
- 未指定時の全履歴名義判定と全履歴secret/path検査は維持する。

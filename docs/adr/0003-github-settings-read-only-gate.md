# ADR-0003: GitHub Settingsはread-only gateと個別承認へ分離する

- Status: Proposed
- Date: 2026-08-24
- Decision owners: repository maintainers

## Context

これまでのrepo-preflightはGitHub設定ガイドと鮮度markerを持つ一方、branch ruleset、Actions権限、security機能などの現在値を機械取得しませんでした。そのため、ガイドを読んだエージェントが設定済みと未設定、403/404、plan制約を混同する余地がありました。

一方、readiness scanへ常時remote取得を入れると、ローカル検査が認証・network依存になります。設定変更まで自動化すると、visibility、Actions権限、ruleset、security機能など影響の異なる操作を包括承認しやすくなります。

## Decision

`configure_settings` intentだけがGitHub APIをGETし、次を行います。

1. originを`OWNER/REPO`へ正規化し、repository identityを照合する。
2. `solo_public`、`team_public`、`high_risk_public`のいずれかと現在値を比較する。
3. 設定ごとに現在値、推奨値、tier、外部影響、rollback、変更APIのpreviewを返す。
4. 403、404、欠落field、plan・権限制約を`false`と推測せず`unavailable`にする。
5. requiredの差分または取得不能だけをintent blockerにする。

このintentは設定変更を実装しません。実行時はfreshな現在値を再取得し、対象repository、正確な操作、外部影響、rollbackを設定ごとに提示して別承認を得ます。通常scanはネットワーク非依存のまま維持します。

## Alternatives considered

### 通常scanで毎回GitHub Settingsを取得する

採用しません。CI、offline環境、未認証環境までremote依存にし、ローカル検査の再現性を下げるためです。

### ガイドだけを維持し、実測は各エージェントへ任せる

採用しません。endpoint詳細の取りこぼしと、取得不能を無効と誤認する可能性を機械的に止められないためです。

### 差分を検出したら自動変更する

採用しません。repository settingsは外部状態を変え、項目ごとに影響とrollbackが異なるためです。

## Consequences

- Settings変更前に再利用できる機械可読packetが得られます。
- ruleset一覧だけでなく詳細、Actionsのselected policy、workflow token権限、security機能を区別して確認できます。
- GitHub CLIと認証が使えない場合、設定intentはfail-closedになります。
- organization / enterprise policyの全体像、実CI成功、設定変更の実行・承認は保証しません。

## Verification

- compliantな`solo_public` profileはGETだけでpassする。
- requiredとrecommendedの差分を分離し、recommendedだけではblockしない。
- 403、欠落field、selected policy詳細の取得不能を`unavailable`にする。
- `team_public`はapproval数1以上を要求する。
- packetの`approved`と`external_actions_performed`は常にfalseから始まる。

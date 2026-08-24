# Architecture Decision Records

repo-preflightで長く維持する設計判断を、ADR（Architecture Decision Record）として記録します。実装方法の説明ではなく、採用した境界、退けた選択肢、将来見直す条件を残す場所です。

## 運用

- file名は `NNNN-short-title.md` とし、番号は4桁の連番にします。
- statusは `Proposed`、`Accepted`、`Superseded`、`Rejected` のいずれかです。
- `Accepted` の判断内容を直接書き換えて意味を反転させません。方針変更時は新しいADRを追加し、旧ADRから参照して `Superseded` にします。
- コード、テスト、運用文書と矛盾する場合は、どれが最新の正本かをPR内で明示します。
- 公開、外部送信、repository visibility、mergeなどの人間承認境界はADRだけで変更できません。

## 一覧

| ADR | status | 判断 |
|---|---|---|
| [0001](0001-github-action-sha-update-exemption.md) | Accepted | GitHub Action参照だけの更新を厳密な条件で関連文書・テスト要件から免除する |
| [0002](0002-pr-self-review-copy-integrity.md) | Accepted | 外部生成のPRセルフレビュー配布物を本文hashで検査し、手編集を止める |

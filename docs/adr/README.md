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
| [0003](0003-github-settings-read-only-gate.md) | Proposed | GitHub Settingsの実測・比較・previewをread-only gateにし、変更を個別承認へ分離する |
| [0004](0004-ai-constitution-entry-contract.md) | Proposed | 共通AI憲法をruntimeごとのpointer/materialized/manual入口契約で検査する |

| [0005](0005-reviewed-secret-exceptions.md) | Proposed | 完全束縛した明示指定の例外で既知secret誤検知だけを除外する |

| [0006](0006-private-evidence-path-exceptions.md) | Accepted | private原資料の個人pathを可視性・期限・完全hash束縛付きの人間レビューで扱う |

| [0007](0007-release-identity-scope.md) | Proposed | releaseの今回名義判定と既存公開履歴の監査を分けて報告する |

| [0008](0008-public-synthetic-path-fixtures.md) | Proposed | 公開の予約済み合成pathだけを期限・完全hash束縛付きの明示policyで扱う |

| [0009](0009-publish-gate-and-ratchet.md) | Proposed | 公開を全検査passと人の確認付きの専用コマンドへ寄せ、CIはラチェットで止める |

| [0010](0010-strict-jpeg-private-scan.md) | Accepted | JPEGをstrict復号し、文字metadataの検査と検査不能時の停止を維持する |
| [0011](0011-existing-jpeg-unverified-does-not-block-submission.md) | Accepted | 送信に含まれない既存JPEGの構造不正は、そのfileだけ未展開として送信全体を止めない |
| [0012](0012-image-binary-and-repository-name-semantics.md) | Accepted | 検証済み画像の符号列・画素とmetadata、Git相対名の意味を区別する |
| [0013](0013-regex-template-source-role.md) | Accepted | 検査用regexのusername classを具体的な個人pathと区別する |
| [0014](0014-media-source-semantics.md) | Accepted | 形式decoderで媒体を検証し、本文・metadataを既存検査へ渡し、未知・未検証領域を拒否する |
| [0015](0015-private-media-metadata-inspection.md) | Proposed | 非公開の制作媒体に形式・metadata・境界sample・全SHAの検査方針を明示する |

- [ADR-0016: 滞留PRの統合と未採用案の整理](0016-backlog-closeout-and-scope.md)

- [ADR-0018: 非公開本文の差分比較前に厳密一致する両端を確定する](0018-bounded-private-text-delta-edges.md) — Proposed。既存の全文解析と不明時の拒否を維持する局所修復候補。

- [ADR-0017: 検出用正規表現とレビュー済み合成負例の区別](0017-release-identity-regex-and-fixtures.md)

- [ADR-0019: coreを維持しPillow媒体機能を安全な実行環境へ限定する](0019-pillow-media-runtime-floor.md) — Proposed。旧decoderの入力前拒否とPython 3.9 coreの分離。

# intent 対話の運用オプション

[AIエージェントから使う](../README.md#aiエージェントから使う) の補足です。質問パケットそのものの契約は README を参照してください。

## 次から出さない (dismiss / snooze)

完璧な設定でなくても運用できるよう、**推奨・任意の再質問**には次の選択肢が付きます。

- `dismiss_30d` — 30日間この項目を出さない
- `dismiss_90d` — 90日間この項目を出さない
- `dismiss_forever` — 次からこの項目は出さない

対話 UI が提示するのは上記です。CLI の `--dismissal-mode` は `7d` / `30d` / `90d` / `forever` を受け付けます（`7d` は直接記録用）。

記録先は採用先リポジトリの `.repo-preflight.json` です。

```bash
python scripts/readiness_scan.py --repo /path/to/your-repo \
  --record-dismissal configure_expected_identity \
  --dismissal-mode forever \
  --dismissal-reason "private only for now"
```

抑止**できない**もの: secret / 個人 path / 必須文書欠落 / dirty worktree / 危険操作の最終確認 など。

## GitHub 更新の反映保証

| 保証すること | 保証しないこと |
|---|---|
| 同梱 `references/github-settings.md` の `last_reviewed` 期限切れを検知し、「ガイドを更新しますか？」を出す | GitHub 製品変更をリアルタイムで自動追従することそのもの |
| 更新手順と公式 docs 入口を文書に持つ | 「常に最新の GitHub 公式と完全一致」という永久保証 |

期限切れ時は intent 対話に `refresh_github_settings_baseline` が出ます。更新後は marker の日付を進めます。

## base ref の scope 指定

既存private repoのpush / PR / mergeでは `--base-ref` を指定すると、今回の変更fileと `base..HEAD` のcommit履歴だけを検査できます。repo全体に以前からある問題を免除する機能ではなく、今回差分とbaselineを別々に報告するためのscope指定です。baseがHEADの祖先でなければ停止します。

公開・releaseでは `--base-ref` を使わず、必須文書と全履歴を含むrepo全体検査が必要です。change-sensitiveな整合性検査だけにbaseが必要なら `--consistency-base-ref` を使います。secret・個人path・必須文書のscopeはrepo全体のままです。

確認packetにはbase ref / base SHA / head SHAが入り、実際のpush / PRは同じbaseへ固定します。baseまたはHEADが変わった場合は、古い結果を使わず再検査します。

# Repo Preflight

**push・PR・共有・公開の直前に、リポジトリの危険物と準備不足を見つける読み取り専用ゲートです。**

APIキー候補や個人PCのパスを現在のファイルとGit履歴から探し、必須文書や変更差分の整合性も確認します。検査結果はJSONで返すため、人間だけでなくAIエージェントやCIからも同じ基準で利用できます。

> [!IMPORTANT]
> `status: pass` は自動検査に合格したという意味です。公開、push、PR、mergeを承認するものではありません。外部へ影響する操作は必ず人間が確認します。

## 30秒でわかる使い方

必要なのはPython 3.11以降とGitだけです。追加パッケージはありません。

```bash
git clone https://github.com/nexus-ai-2045/repo-preflight.git
cd repo-preflight
python scripts/readiness_scan.py --repo /path/to/your-repo
```

操作の直前に人間確認まで含める場合は、`--intent` と `--human` を指定します。

```bash
python scripts/readiness_scan.py \
  --repo /path/to/your-repo \
  --intent open_pr \
  --base-ref origin/main \
  --human
```

結果の読み方は3種類です。

| `status` | 意味 | 次にすること |
|---|---|---|
| `pass` | CLIが担当する検査に合格 | 人間が内容と操作対象を確認する |
| `blocked` | 修正または人間の回答が必要 | findings / proposalsを処理して再検査する |
| `tool_error` | Gitやファイルを十分に検査できなかった | 原因を直すまで結果を採用しない |

## どの場面で使うか

public化専用ではありません。見せる相手が増える直前に使います。

| 場面 | 推奨intent | 主に確認すること |
|---|---|---|
| リポジトリ作成 | `create_repo` | visibility、初期文書、名義 |
| push | `push` | 今回差分、履歴、dirty状態 |
| PR作成 | `open_pr` | baseとの差分、文書・テスト整合性 |
| merge | `merge` | 統合前の残務と人間確認 |
| チーム共有・納品・公開 | `publish` | audience、全履歴、権利・個人情報 |
| release準備 | `release` | README情報設計と運用証拠 |

## 検査するもの

- APIキーなどのsecret候補と個人パス。削除済みファイルを含むGit履歴も対象
- README、LICENSE、SECURITY.md、PREFLIGHT.mdなどの必須文書
- commitの作者・committer名義
- Markdownリンク、READMEの宣言、docs / tests更新、生成物hashの整合性
- GitHub設定ガイドや運用証拠の不足

## 検査しないもの

- 公開、push、PR、merge、visibility変更そのもの
- 公開権利や個人情報の最終判断
- 依存ライブラリの最新脆弱性監査
- remote CI、branch protection、GitHub権限の現在状態
- 「秘密情報が絶対に存在しない」という完全保証

## READMEの案内

- まず試す: [クイックスタート](#クイックスタート)
- AIエージェントへ組み込む: [AI 実装フロー](#ai-実装フロー--intent-対話-本体)
- PRごとの文書整合性を検査する: [PR マージ前の整合性ゲート](#pr-マージ前の整合性ゲート)
- 判定の境界を確認する: [保証すること / 保証しないこと](#保証すること--保証しないこと)
- Claude Code / Grok / Codexで使う: [Skill](#skill-claude-code--grok--codex)
- v0.1.xから移行する: [v0.1.x からの移行](#v01x-からの移行)

## 目的

機械が確認できた事実と、人間が判断すべき公開・共有の可否を分離します。`status` は自動検査の結果だけを表し、`publication_decision` は常に人間レビューを要求します。

## PR マージ前の整合性ゲート

`.repo-preflight-consistency.json` を置くと、既存の `readiness_scan.py` が Markdown リンク、README の宣言済みコマンドと file、変更コードに対する docs / tests 更新、SSOT・生成物の SHA-256 ドリフトを追加検査します。repo 固有側は宣言だけで、共通ロジックは `repo-preflight` に残ります。

repo-preflight自身は設定を `enforce` に固定し、README可読性と文書整合性をLinux、macOS、WindowsのPR、merge queue、main pushで必須検査します。設定削除やmode弱体化もCI失敗になります。意味的な正しさは人間レビューが必要です。

導入は `shadow` から始め、観測した誤検知を影響マップで調整し、`ratchet` で新規悪化を止めてから `enforce` に切り替えます。変更内容に応じた可読性・security・GitHub等の capability 推薦も、人間レビュー要求付きでレポートします。設定例と運用境界は [リポジトリ整合性ゲート](docs/repository-consistency-gate.md) を参照してください。

設定は未知キーや型違いをfail-closedで拒否し、Gitのsymlink・gitlinkを追跡してrepo外を読まない境界を持ちます。

## 保証すること / 保証しないこと

対話モードでも非対話モードでも、次の境界は同じです。JSON report にも `guarantees` / `non_guarantees` として入ります。

### できること — 保証する範囲

| 対象 | 内容 |
|---|---|
| 読み取り専用検査 | ローカルGitの現在treeと履歴を変更せず読む |
| 自動判定項目 | 必須文書・secret候補・個人path・作者名義・CI設定の最低限の構造 |
| status の意味 | pass / blocked / tool_error はCLI担当分の結果だけ |
| 公開判定の分離 | `publication_decision` は常に人間レビュー要求。自動承認しない |
| 秘密値の非出力 | 検出結果に秘密値そのものを載せない |

削除済みのファイルも履歴に残っていれば検出します。

### 制約 — 保証しない範囲 (別証拠・人間判断が要る)

| 対象 | なぜCLIで判定しないか |
|---|---|
| 秘密情報が「存在しない」ことの完全保証 | 独自形式・符号化・大容量blob・バイナリ内は見逃し得る |
| 依存ライブラリの既知脆弱性 | エコシステム固有の最新監査が要る |
| 第三者素材を公開する権利 | 法的判断 |
| branch保護・review必須設定 | GitHub側の現在状態 |
| CIが実際に成功したか | remoteの実行結果 |
| 障害の通知先、復旧手順 | 実際に試した記録が要る |
| README・個人情報・公開範囲の目視 | 人間の確認 |
| 公開・push・merge・visibility変更 | 実行機能を持たない |

内蔵の正規表現は代表的な秘密情報の形式を検出する補助機能です。
専門のsecret scanner、依存関係の監査、人間レビューを必ず併用してください。

## クイックスタート

PyPIには公開していません。このリポジトリを取得して使います。
追加の依存はなく、Python 3.11以降とgitがあれば動きます。

```bash
git clone https://github.com/nexus-ai-2045/repo-preflight.git
cd repo-preflight
```

### AI 実装フロー — intent 対話 (本体)

エージェントが次の操作に進む**直前**に自動発火します。ユーザーが毎回「preflightして」と言う必要はありません。

| これからやること | コマンド |
|---|---|
| リポジトリ新規作成 | `python scripts/readiness_scan.py --intent create_repo --human` |
| push | `python scripts/readiness_scan.py --repo PATH --intent push --base-ref origin/BASE --human` |
| PR 作成 | `python scripts/readiness_scan.py --repo PATH --intent open_pr --base-ref origin/BASE --human` |
| merge | `python scripts/readiness_scan.py --repo PATH --intent merge --human` |
| 公開 / 共有 / 納品 | `python scripts/readiness_scan.py --repo PATH --intent publish --audience public --human` |
| release 準備 | `python scripts/readiness_scan.py --repo PATH --intent release --human` |

返ってくるのは `repo-preflight.dialogue/v3` の「質問パケット」です。エージェントは `proposals` と `confirmations` を人間へ番号付きで転記し、回答があるまで外部操作しません。

質問の例:

- 「新しい GitHub リポジトリは private で作成しますか？」
- 「SECURITY.md がありません。テンプレートから作成しますか？」
- 「作者名義の固定照合が未設定です。設定しますか？」
- 「未コミットの変更があります。コミットしてから進めますか？」

secret や個人 path の検出時は **「無視して進む」選択肢を出しません**。

既存private repoのpush / PRでは `--base-ref` を指定すると、今回の変更fileと
`base..HEAD` のcommit履歴だけを検査できます。repo全体に以前からある問題を免除する機能ではなく、
今回差分とbaselineを別々に報告するためのscope指定です。baseがHEADの祖先でなければ停止します。
公開・releaseでは使えず、必須文書と全履歴を含むrepo全体検査が必要です。
確認packetにはbase ref / base SHA / head SHAが入り、実際のpush / PRは同じbaseへ固定します。
baseまたはHEADが変わった場合は、古い結果を使わず再検査します。

#### 次から出さない (dismiss / snooze)

完璧な設定でなくても運用できるよう、**推奨・任意の再質問**には次の選択肢が付きます。

- `dismiss_30d` — 30日間この項目を出さない
- `dismiss_90d` — 90日間この項目を出さない
- `dismiss_forever` — 次からこの項目は出さない

記録先は採用先リポジトリの `.repo-preflight.json` です。

```bash
python scripts/readiness_scan.py --repo /path/to/your-repo \
  --record-dismissal configure_expected_identity \
  --dismissal-mode forever \
  --dismissal-reason "private only for now"
```

抑止**できない**もの: secret / 個人 path / 必須文書欠落 / dirty worktree / 危険操作の最終確認 など。

#### GitHub 更新の反映保証

| 保証すること | 保証しないこと |
|---|---|
| 同梱 `references/github-settings.md` の `last_reviewed` 期限切れを検知し、「ガイドを更新しますか？」を出す | GitHub 製品変更をリアルタイムで自動追従することそのもの |
| 更新手順と公式 docs 入口を文書に持つ | 「常に最新の GitHub 公式と完全一致」という永久保証 |

期限切れ時は intent 対話に `refresh_github_settings_baseline` が出ます。更新後は marker の日付を進めます。

### 素の検査 (CI / 現状把握)

`--repo` だけなら従来どおり検査JSONです。

対象repoの整合設定に `impact_map` やsource付き生成物がある場合、変更差分の判定には `push` / `open_pr` intentと `--base-ref` が必要です。baseなしでは `change_sensitive_scope_unavailable` としてfail-closedになります。

```bash
python scripts/readiness_scan.py --repo /path/to/your-repo
```

```json
{
  "schema": "repo-preflight.scan/v3",
  "status": "pass",
  "publication_decision": "blocked_human_review_required",
  "repo": "your-repo",
  "guarantees": ["..."],
  "non_guarantees": ["..."],
  "checks": {
    "secret_scan": { "status": "pass", "finding_count": 0 },
    "required_documents": { "status": "pass", "missing": [], "invalid": [] }
  }
}
```

### オプション

| オプション | 用途 |
|---|---|
| `--intent <name>` | AI操作直前ゲート。create_repo / push / open_pr / merge / publish / release |
| `--repo <path>` | 検査対象。create_repo 以外では必須 |
| `--release` | README情報設計ゲートも同時に実行する |
| `--expected-identity "<Name> <mail>"` | 全commitの作者/committerが指定の名義かを検査する |
| `--audience <key>` | 見せる相手 (public/team/client/collaborator/local) |
| `--human` | 質問文/要約をstderr、JSONをstdoutへ |
| `--interactive` / `-i` | コンソール補助 (本体ではない) |

公開名義を統一しているリポジトリでは、履歴に個人名義が混ざっていないかを確認できます。

```bash
python scripts/readiness_scan.py --repo /path/to/your-repo \
  --expected-identity "Example <dev@example.invalid>"
```

### 終了コード

| コード | 意味 |
|---|---|
| `0` | 検査 pass、または intent 対話が `ready_after_confirmation` |
| `1` | 検査 blocked、または intent が `needs_human_input` / `blocked`（人間の回答待ち） |
| `2` | Gitや履歴取得など検査自体が失敗 |

CLIは既定で読み取り専用です。リポジトリ作成、push、PR、merge、visibility変更、投稿は行いません。
`--release` もREADMEやreleaseを自動で変更・作成しません。

## 見せる相手を広げる流れ

public、team、client、collaboratorでは必要な証拠が異なります。どの場合も承認 → 実測 → 再確認の順に進め、private保存、PRまで、mergeまでも正規の完了地点として扱います。詳しい状態遷移と証拠は [状態一覧](references/lifecycle.md) を参照してください。

## v0.1.x からの移行

v0.2.0 でリポジトリ名 (`public-readiness` → `repo-preflight`) と、必須のレビュー記録
(`PUBLIC_READY.md` → `PREFLIGHT.md`) が変わりました。
手順は [v0.1.x から v0.2.0 への移行](docs/migration-v0.1-to-v0.2.md) を読んでください。

## Skill (Claude Code / Grok / Codex)

| Runtime | 入口 |
|---|---|
| 正本 | [SKILL.md](SKILL.md) |
| Claude Code | [runtime/claude-code/SKILL.md](runtime/claude-code/SKILL.md) |
| Grok | [runtime/grok/SKILL.md](runtime/grok/SKILL.md) |
| Codex | [runtime/agents/openai.yaml](runtime/agents/openai.yaml) |

保証範囲（何が機械検証され、何が運用契約か）は
[docs/runtime-support.md](docs/runtime-support.md)。

```bash
# このマシンで CLI + skill 入口の最小保証を確認
python scripts/runtime_smoke.py --repo .

# Claude Code / Grok の skills へ portable 配布 (確認のみ → 書込)
python scripts/install_runtime_skills.py --repo .
python scripts/install_runtime_skills.py --repo . --apply

# install 後は skill 側 launcher を使う (絶対 path 不要)
python ~/.claude/skills/repo-preflight/run_preflight.py --intent create_repo --human
```

skill は **各自が clone したうえで `--apply`** する。他人の skill フォルダをコピーしない。

公開リポジトリのruleset、merge方式、Actions権限、security機能の選択理由は
[GitHub repository設定ガイド](references/github-settings.md) を参照してください。

## License

MIT License。詳細は [LICENSE](LICENSE) を参照してください。

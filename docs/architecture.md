# アーキテクチャ

## 目的

Repo Preflightは、ローカルGit repositoryを読み取り、見せる相手を広げる判断に必要な証拠を構造化するローカルファーストのscanner兼ライフサイクルskillです。scanner自身はGitHub作成、push、visibility変更、投稿を行いません。

## コンポーネント

- `scripts/readiness_scan.py`: Git状態、必須文書、履歴、secret候補、個人path、作者名義、CI、originを検査するread-only CLI。v0.3 では `--intent` で操作直前の質問パケットも返す
- `scripts/dialogue_gate.py`: AI向け intent 対話パケット (proposals / confirmations) を組み立てる
- `SKILL.md`と`references/`: 状態、承認手順、必要文書、repository catalog登録の仕様。AI自動発火トリガーを定義
- `tests/`: 一時Git repositoryを使い、履歴secret、読取不能、壊れたGit object、非ASCII path、gitlink、intent対話などのfail-closed挙動を固定
- `assets/`: 対象repoへ明示的に適用する文書テンプレート。scannerから自動上書きしない
- `.github/workflows/ci.yml`: ubuntu / macOS で Python 3.11・3.13、Windows で 3.13 の回帰試験（ubuntu のみ black check も実行）

## データフロー

```text
AI is about to create_repo / push / open_pr / merge / publish / release
  -> readiness_scan.py --intent <intent> [--repo PATH]
  -> (optional) local scan
  -> dialogue_gate: proposals + confirmations + guarantees/non_guarantees
  -> agent presents numbered questions to human
  -> human answers
  -> agent applies only approved local fixes / settings
  -> separate approval for the external execute step
  -> verify
```

素の検査だけの場合:

```text
--repo PATH
  -> resolve Git top-level
  -> fixed-argument git subprocess
  -> inventory + bounded blob reads
  -> regex + metadata checks
  -> schema v3 JSON on stdout
```

scannerは検査対象の内容を外部送信しません。secret本文をreportへ含めず、候補ファイル位置だけを返します。
保証境界 (`guarantees` / `non_guarantees`) は scan / dialogue のどちらでも同じ定義を使い、pass を公開承認と誤読させない。
intent 対話は設定提案までで、push/PR/public の実行そのものは別承認境界に残す。

## Trust boundary

- 信頼しない入力: `--repo`で指定されたrepository、ファイル名、ファイル内容、Git履歴、remote URL、Git command output
- 信頼する境界: ローカルPython runtime、ローカルGit executable、scannerの固定コマンド引数、実行ユーザーのfilesystem権限
- 外部境界: GitHub状態や実CI結果はscanner単体では証明せず、`gh`等による現在状態の確認を要求する
- 権限境界: scannerは読み取り専用とし、commit、push、設定変更、削除、visibility変更を実装しない

## 制約

- regex検出は専門secret scannerと人間レビューを置き換えない
- 2 MBを超える履歴blobは内容検査対象外であり、別の大容量ファイル検査が必要
- symlinkとgitlinkはリンク先を読まず、履歴objectとmetadataだけを扱う
- GitHubのbranch保護、Security Advisories、依存関係の脆弱性、第三者素材の権利は別途確認する

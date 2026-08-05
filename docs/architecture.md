# アーキテクチャ

## 目的

Repo Preflightは、ローカルGit repositoryを読み取り、見せる相手を広げる判断に必要な証拠を構造化するローカルファーストのscanner兼ライフサイクルskillです。scanner自身はGitHub作成、push、visibility変更、投稿を行いません。

## コンポーネント

- `scripts/readiness_scan.py`: Git状態、必須文書、履歴、secret候補、個人path、作者名義、CI、originを検査するread-only CLI
- `SKILL.md`と`references/`: 状態、承認手順、必要文書、repository catalog登録の仕様
- `tests/`: 一時Git repositoryを使い、履歴secret、読取不能、壊れたGit object、非ASCII path、gitlinkなどのfail-closed挙動を固定
- `assets/`: 対象repoへ明示的に適用する文書テンプレート。scannerから自動上書きしない
- `.github/workflows/ci.yml`: Linux上のPython 3.11/3.13でformatterと回帰試験を実行

## データフロー

```text
user supplied repo path
  -> resolve Git top-level
  -> fixed-argument git subprocess
  -> tracked/untracked inventory + Git object inventory
  -> bounded blob reads
  -> regex candidate detection + repository metadata checks
  -> JSON report to stdout
```

scannerは検査対象の内容を外部送信しません。secret本文をreportへ含めず、候補ファイル位置だけを返します。

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

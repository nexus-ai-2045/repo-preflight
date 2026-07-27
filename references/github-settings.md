# GitHub repository設定ガイド

この文書は、公開repositoryで確認するGitHub設定と選択理由を整理する。特定repositoryの現在値や検査記録は対象repository側または非公開の運用記録へ保存し、この文書には含めない。

## 推奨度

| 区分 | 意味 |
|---|---|
| 必須 | 無効なら公開・mergeを止め、理由を解消する |
| 推奨 | 通常は採用する。採用しない場合は理由を記録する |
| 任意 | 開発人数、変更頻度、運用方法に合わせて選ぶ |

設定値は保存記録だけで判断せず、公開前、重要なmerge前、運用変更後にGitHub画面またはAPIで再測定する。

## 基本設定

| 設定 | 推奨度 | 通常の選択 | 選択理由と例外 |
|---|---|---|---|
| repository visibility | 必須 | 公開承認まではprivate | public化するとfilesとcommit historyが外部から見える。対象repository固有の承認を取る |
| default branch | 必須 | `main`など1本へ固定 | ruleset、CI、releaseの基準を一意にする |
| Automatically delete head branches | 推奨 | ON | merge済みの短期branchを残さない。継続利用するrelease branchなどがある場合はOFF |
| Issues | 任意 | 問い合わせを受けるならON | SECURITY.mdの報告経路とは分ける |
| Projects / Wiki / Discussions | 任意 | 使用するものだけON | 未使用機能を無理に公開面へ増やさない |

branch自動削除はGitHub上のremote head branchだけを対象とする。local branchとworktreeの整理は別工程である。

## rulesetとmerge

| 設定 | 推奨度 | 通常の選択 | 選択理由と例外 |
|---|---|---|---|
| default branchの削除禁止 | 必須 | 有効 | 誤削除を防ぐ |
| force push禁止 | 必須 | 有効 | review済み履歴の差し替えを防ぐ |
| PR経由の変更 | 推奨 | 有効 | diff、CI、review証拠を残す |
| 必須status checks | 必須 | test、lint、security checkを指定 | check名のdriftで保護が空洞化していないか確認する |
| branchを最新にしてからmerge | 推奨 | 有効 | stale base上の成功判定を避ける |
| review thread解決 | 推奨 | 有効 | 未解決指摘を残したmergeを防ぐ |
| 承認review数 | 条件付き推奨 | teamは1以上、soloは0でも可 | solo repositoryで自己承認不能な停止状態を作らない |
| merge方式 | 任意 | 小規模repositoryはsquash中心 | merge commitやrebaseを使う理由がある場合は複数方式を許可する |
| auto-merge | 任意 | 初期はOFF | PR数が多くCI待ち後のmerge忘れが負担になった場合に検討する |

auto-mergeは全PRを自動でmergeする設定ではない。各PRで個別に指定し、必須reviewとstatus checksを通過した後に実行される。それでも最終mergeを人が明示実行したい運用ではOFFを維持する。

## GitHub Actions

| 設定 | 推奨度 | 通常の選択 | 選択理由と例外 |
|---|---|---|---|
| Workflow permissions | 必須 | `contents: read`を基準 | job単位で必要な権限だけ追加する |
| PR作成・承認権限 | 推奨 | OFF | workflowからの意図しないrepository変更を減らす |
| action参照 | 必須 | full commit SHAへ固定 | tag差し替えによるsupply-chain riskを減らす |
| Require actions to be pinned to a full-length commit SHA | 推奨 | 利用可能ならON | workflow内のSHA固定規律をGitHub設定でも強制する |
| Allowed actions | 条件付き推奨 | GitHub製と明示許可したactionへ限定 | 外部actionが増えるほど保守負荷も増える |

workflow内に`permissions:`を明記する。repository既定権限だけへ依存しない。外部binaryをdownloadする場合はversionとchecksumを固定する。

公式資料:

- [GitHub Actions設定](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
- [Actions permissions REST API](https://docs.github.com/en/rest/actions/permissions)

## securityと依存関係

| 設定 | 推奨度 | 通常の選択 | 選択理由と例外 |
|---|---|---|---|
| SECURITY.md | 必須 | 報告方法と対応範囲を記載 | 公開Issueへsecretや脆弱性詳細を書かせない |
| Dependabot alerts | 必須 | ON | 既知の脆弱な依存関係を検知する |
| Dependabot security updates | 推奨 | ON | 修正候補をPRで受け取る |
| Dependabot version updates | 推奨 | ecosystemごとに設定 | Python依存とGitHub Actionsの通常更新を継続する |
| Code scanning / CodeQL | 推奨 | 対応言語でON | default branchとPRで解析結果を確認する |
| Secret scanning | 必須 | 利用可能ならON | Git履歴内の既知secret patternを検知する |
| Push protection | 必須 | 利用可能ならON | secret候補のpushを事前に止める |
| Private vulnerability reporting | 推奨 | public repositoryではON | 研究者が非公開で構造化された報告を送れる |

Dependabot version updatesは`.github/dependabot.yml`で管理する。最低限、利用するpackage ecosystemと`github-actions`を対象にし、更新頻度と同時PR数をrepositoryの保守余力に合わせる。

公式資料:

- [Dependabot version updates](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-version-updates)
- [GitHub ActionsをDependabotで更新](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions)
- [Private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)

## 運用profile

### solo / 小規模

- PR、必須CI、thread解決を要求する。
- approval数は0でもよい。人間が最終mergeを実行する。
- squash mergeとbranch自動削除を基本にする。
- auto-mergeはmerge待ちが実害になるまでOFFにする。

### 複数maintainer

- approvalを1以上にする。
- CODEOWNERSと担当範囲を検討する。
- last pushを別reviewerが承認する設定を検討する。
- bypass actorを最小化し、利用理由を記録する。

### 高リスク / 外部利用が多い

- security check、dependency review、release署名・provenanceを追加する。
- Actionsを許可listへ制限し、full SHA固定を設定でも強制する。
- private vulnerability reportingと通知先を運用確認する。
- rollbackとsecurity advisory対応を定期的に試す。

## read-only確認例

PowerShellでは`&`を含むAPI URL全体を引用する。

```powershell
gh api repos/OWNER/REPO --jq `
  '{visibility,default_branch,delete_branch_on_merge,allow_squash_merge,allow_merge_commit,allow_rebase_merge,allow_auto_merge,security_and_analysis}'

gh api repos/OWNER/REPO/rulesets
gh api repos/OWNER/REPO/actions/permissions
gh api repos/OWNER/REPO/actions/permissions/workflow
gh api repos/OWNER/REPO/private-vulnerability-reporting

gh api 'repos/OWNER/REPO/code-scanning/alerts?state=open&per_page=100'
gh api 'repos/OWNER/REPO/dependabot/alerts?state=open&per_page=100'
gh api 'repos/OWNER/REPO/secret-scanning/alerts?state=open&per_page=100'
```

APIが404や権限errorを返した項目を無効と断定しない。classic branch protectionとrulesetは別に確認し、organization / enterprise policyによる上書きも区別する。

## 判断記録

対象repository側の記録には次を残す。

```text
setting:
observed_value:
observed_at:
source:
recommendation_tier:
decision:
rationale:
external_effect:
rollback:
reviewed_by:
```

設定変更は`inspect -> preview -> approval -> execute -> verify`で行う。複数設定を包括承認へまとめず、外部から見える範囲や自動化の副作用が異なる設定は分けて確認する。

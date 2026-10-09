# GitHub repository設定ガイド

<!-- repo-preflight:github-baseline last_reviewed: 2026-10-09 max_age_days: 90 -->

この文書は、公開repositoryで確認するGitHub設定と選択理由を整理する。特定repositoryの現在値や検査記録は対象repository側または非公開の運用記録へ保存し、この文書には含めない。

## 鮮度と更新の保証境界

| 区分 | 内容 |
|---|---|
| 保証すること | `last_reviewed` と `max_age_days` を機械判定し、期限切れなら intent 対話で「ガイドを更新しますか？」を出す |
| 保証しないこと | GitHub 製品変更・公式推奨のリアルタイム自動追従そのもの |
| 更新トリガ | public/release 前後、GitHub の security / ruleset / Actions 周りに大きな変更があったと気づいたとき、または age が max を超えたとき |
| 更新手順 | 公式 docs / changelog を確認 → この文書の表を直す → 上記 marker の `last_reviewed` を今日にする → 必要なら CHANGELOG に1行 |

公式の入口 (更新時に再確認):

- [GitHub Changelog](https://github.blog/changelog/)
- [Repository security and analysis](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository)
- [Rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- [GitHub Actions security hardening](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions)

## 推奨度

| 区分 | 意味 |
|---|---|
| 必須 | 無効なら公開・mergeを止め、理由を解消する |
| 推奨 | 通常は採用する。採用しない場合は理由を記録する |
| 任意 | 開発人数、変更頻度、運用方法に合わせて選ぶ |

設定値は保存記録だけで判断せず、公開前、重要なmerge前、運用変更後にGitHub画面またはAPIで再測定する。

## 初期値と変更要否

次はGitHub REST APIでrepositoryを作成する場合の一般的な初期値である。GitHub画面、template repository、organization / enterprise policy、plan、作成時の指定により変わる。初期値を現在値として扱わず、必ず対象repositoryをread-onlyで確認する。

| 設定 | 一般的なAPI初期値 | 通常の推奨 | 変更要否 |
|---|---:|---:|---|
| visibility | public | 承認まではprivate | 必須。安全側へ明示指定する |
| Issues | ON | 問い合わせを受けるならON | 用途に合わせる |
| Projects | ON | 未使用ならOFF | 任意 |
| Wiki | ON | 未使用ならOFF | 任意 |
| Discussions | OFF | community運用時だけON | 通常は変更不要 |
| squash merge | ON | ON | 通常は変更不要 |
| merge commit | ON | repository方針による | squashだけにするならOFF |
| rebase merge | ON | repository方針による | squashだけにするならOFF |
| auto-merge | OFF | 初期はOFF | 通常は変更不要 |
| merge後のhead branch自動削除 | OFF | 短期branch運用ではON | 推奨変更 |
| PR branchの更新許可 | OFF | 必要時にON | 任意 |

GitHub Actionsの既定token権限、ruleset、security機能は、organization / enterprise policyやpublic / privateで差があるため単一の初期値を置かない。APIで`observed_value`を取得し、推奨値との差だけを変更候補にする。

public repositoryではsecret scanningなど一部機能が自動提供される場合がある。一方、repository単位のpush protectionやprivate vulnerability reportingなどは明示的な有効化が必要な場合がある。画面表示とAPIの現在値を優先する。

公式資料:

- [Repository REST APIの初期値](https://docs.github.com/en/rest/repos/repos)
- [Security and analysis設定](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository)

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

default branchの保護は、rulesetとclassic branch protectionのどちらで掛けてもよい。両方が掛かった場合は全ての規則が強制され、同じ規則が違う強さで定義されていれば厳しい方が効く（[About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)）。repo-preflightも両方を読み、累積して評価する。packetの`default_branch_ruleset`、`ruleset_deletion_protection`、`ruleset_non_fast_forward_protection`、`ruleset_pull_request`、`required_review_thread_resolution`、`strict_required_status_checks_policy`は、classic branch protectionで満たされた場合も満たされたものとして扱い、`enforced_by`に由来（`ruleset:<id>`または`classic_branch_protection`）を、確認できなかった情報源を`sources_unavailable`に示す。`ruleset_bypass_actors`と`required_status_checks`は`enforced_by`を持たず、前者は`observed_value`の各要素の`ruleset_id`または`source`に、後者は`observed_value.evidence_pull_requests`に由来を示す。名前はpacket schema v1との互換のために変えていない。

| 設定 | 推奨度 | 通常の選択 | 選択理由と例外 |
|---|---|---|---|
| default branchの保護 | 必須 | rulesetまたはclassic branch protectionの一方以上 | 保護が無いと、以下の規則が一つも効かない |
| default branchの削除禁止 | 必須 | 有効 | 誤削除を防ぐ |
| force push禁止 | 必須 | 有効 | review済み履歴の差し替えを防ぐ |
| PR経由の変更 | 推奨 | 有効 | diff、CI、review証拠を残す |
| 必須status checks | 必須 | test、lint、security checkを指定 | check名のdriftで保護が空洞化していないか確認する |
| branchを最新にしてからmerge | 推奨 | 有効 | stale base上の成功判定を避ける |
| review thread解決 | 推奨 | 有効 | 未解決指摘を残したmergeを防ぐ |
| 承認review数 | 条件付き推奨 | teamは1以上、soloは0でも可 | solo repositoryで自己承認不能な停止状態を作らない |
| bypass actor | 条件付き推奨 | 無し、または理由を記録した最小限 | 高リスクでは必須。classic protectionでは、管理者へ適用しない設定（`enforce_admins`が無効）とPR要件のbypass許可もbypassとして数える |
| merge方式 | 任意 | 小規模repositoryはsquash中心 | merge commitやrebaseを使う理由がある場合は複数方式を許可する |
| auto-merge | 任意 | 初期はOFF | PR数が多くCI待ち後のmerge忘れが負担になった場合に検討する |

auto-mergeは全PRを自動でmergeする設定ではない。各PRで個別に指定し、必須reviewとstatus checksを通過した後に実行される。それでも最終mergeを人が明示実行したい運用ではOFFを維持する。

必須status checksの名前は、PRでしか走らないcheck（例: `pr-body-hygiene`）があるため、default branchのHEADではなく、直近にmergeされたPRのhead commitのcheck-runsとcommit statusで照合する。直近のPRがpaths filterなどで一部のcheckを実行しないことがあるので、新しい順に最大5件のmerge済みPRを見る。merge済みPRが無い、またはAPIが取得できない場合は`確認不能`にし、`false`と推測しない。情報源（rulesetとclassic protection）のどれかが`確認不能`の間は、必須checkが照合できても満たしたとは言わない。

読めない情報を、満たしている側へ倒さない。rulesetは、`~DEFAULT_BRANCH`だけをincludeしてexcludeが空のactiveなものだけをdefault branchの保護として数える。それ以外の対象指定（`~ALL`、`refs/heads/main`の直接指定、glob、exclude付き）は解釈せず、その情報源を`確認不能`にする。rulesetの一覧は`per_page=100`で読み、上限まで埋まっていたら`確認不能`にする。rulesetの詳細に`bypass_actors`が返らないときは、bypassが無いとは読まず`確認不能`にする（[Get a repository ruleset](https://docs.github.com/en/rest/repos/rules)に、`bypass_actors`はrulesetへの書き込み権限が無いと返らないとある）。

### 2026-10-09の見直しで確認した点

- security checkの置き方: 必須status checksへ入れる「security check」は、CodeQLのcheckを必須checkにするか、rulesetの「Require code scanning results」ruleで必須toolにCodeQLを指定して満たす。このruleは、必須toolの未設定、実行中、定めたseverity以上のalertがあるときにmergeを止める。check名は、対象repositoryのPRで実際に出る名前を確かめてから指定する。出典: [Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) / [Set code scanning merge protection](https://docs.github.com/en/code-security/code-scanning/managing-your-code-scanning-configuration/set-code-scanning-merge-protection)
- classic protectionのruleset変換: 2026-08-11から、Settings → Branchesのclassic ruleにある「Convert to ruleset」で、必須review、status checks、push制限を同等のruleset規則へ変換できる。classicとrulesetは併存して全て強制されるため、classicで上の必須項目を満たしていれば移行は任意。複数branchへのpattern適用、組織単位の管理、bypass権限の細分化が必要になった時に検討する。出典: [GitHub Changelog 2026-08-11](https://github.blog/changelog/2026-08-11-automatically-migrate-branch-protection-rules-to-repository-rulesets) / [About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- Copilot code reviewの承認（2026-09-01、public preview）: 管理者が許可した場合に限り、Copilotの承認がrepositoryの必須approvalsに数えられる。既定は承認しない。新しいcommitをpushすると、人間の承認と同じくdismissされる。solo profileは承認数0が基本なので、許可する必要は通常無い。承認数を1以上にしてCopilotの承認で満たす運用にするなら、人間の最終確認が外れる点を判断記録へ残す。出典: [GitHub Changelog 2026-09-01](https://github.blog/changelog/2026-09-01-copilot-code-review-can-now-approve-pull-requests)

## GitHub Actions

| 設定 | 推奨度 | 通常の選択 | 選択理由と例外 |
|---|---|---|---|
| Actions実行 | 必須 | 有効 | 無効だとrequired CIが走らず、必須checkを満たせない |
| Workflow permissions | 必須 | `contents: read`を基準 | job単位で必要な権限だけ追加する |
| PR作成・承認権限 | 推奨 | OFF | workflowからの意図しないrepository変更を減らす。この設定は`GITHUB_TOKEN`によるPRの承認だけでなく作成も許可するため、OFFにすると`GITHUB_TOKEN`でPRを作るworkflow（release-pleaseなど）が止まる。その場合はONを維持して理由を記録するか、PR作成をGitHub App tokenへ移してからOFFにする |
| action参照 | 必須 | full commit SHAへ固定 | tag差し替えによるsupply-chain riskを減らす |
| Require actions to be pinned to a full-length commit SHA | 推奨 | 利用可能ならON | workflow内のSHA固定規律をGitHub設定でも強制する。有効にする前の順序は下記 |
| Allowed actions | 条件付き推奨 | GitHub製と明示許可したactionへ限定 | 外部actionが増えるほど保守負荷も増える |
| GitHub製actionの許可 | 必須 | Allowed actionsをselectedにする場合は有効 | `actions/`と`github/`のactionを実行できなくなり、CIが止まる |
| verified creator全体の許可 | 推奨 | 無効 | Marketplaceのverified creator全体ではなく、必要なactionだけを許可する |
| 許可listのpattern | 推奨 | workflowで実際に使う第三者actionだけ | 導出の手順は下記 |

workflow内に`permissions:`を明記する。repository既定権限だけへ依存しない。外部binaryをdownloadする場合はversionとchecksumを固定する。

### Actions設定をAPIで変更するときの作り方

- `actions/permissions`、`actions/permissions/workflow`、`actions/permissions/selected-actions`のPUTは、bodyの項目をまとめて置き換える。観測時の値を固定bodyに埋めて複数の変更を順に実行すると、先の変更を戻す。例えば`sha_pinning_required`を有効にした後で、観測時の`false`を埋めた`allowed_actions`のPUTを実行すると、有効にした設定が`false`へ戻る。
- 変更は「実行直前にGETで取り直した現在値へ、承認された項目の変更だけを重ねて1回PUTする」。同じendpointで複数の項目を承認したときは、重ねる変更を合成して1回で送る。repo-preflightのpacketも固定bodyではなく、`fresh_read`、`copy_from_fresh_read`、`overlay`でこの手順を示す。
- `allowed_actions`を`all`から`selected`へ変えるときは2段にする。`all`の間は`selected-actions` endpointが409を返す（`All actions and workflows are allowed on this repository`）ため、先に`selected`へ切り替え、その後に許可listを設定する。docsにも、`selected-actions`のPUTは`allowed_actions`が`selected`であることが前提と書かれている。
- 許可listはrepositoryの`.github/workflows`で実際に使われているactionから導く。`actions/`と`github/`のactionは`github_owned_allowed`、それ以外は`OWNER/REPO@*`（サブディレクトリのactionや再利用workflowは`OWNER/REPO/PATH@*`）をpatternにする。`verified_allowed`は`false`にする。`./`のlocal actionは許可が要らない。`docker://`や式を含む参照は導出できないので人が判断する。repo-preflightが読むのは`.github/workflows`直下のfileだけで、`.github/actions`のcomposite action内の`uses`や、第三者actionが内部で使うactionは含まない。実行して拒否されたものは追加する。
- 既に`selected`のときは、workflowが使う参照を既存のpatternが覆っているかで判定する。`*`は`/`を越えない保守的な照合で、`!`の拒否patternも見る。覆っていれば満たしているとし、足りない分だけを追加する案にする。既存のpatternは消さず、SHA固定のpatternを`@*`へ緩めない。`local_only`は`selected`より厳しいので、緩める案を出さない。workflowを読めないときは、許可listの項目を`確認不能`にする。
- `docker://`や式を含む参照、走査していないlocal action（`./`、`$/`）があるときは、切り替え案を`ready: false`にして理由を出す。

### `sha_pinning_required`を有効にする前の順序

1. workflowの全ての`uses:`をfull commit SHAへ固定する。再利用workflowはtag参照のままでも通る。
2. `.github/dependabot.yml`に`github-actions`を入れ、SHAの更新を自動化する。
3. その後で`Require actions to be pinned to a full-length commit SHA`を有効にする。

この設定を有効にすると、組織のactionやGitHub製のactionを含め、全てのactionがfull commit SHAでないと使えなくなる。先に有効にすると、tagやbranchで参照しているactionを含むworkflowが失敗する。

### workflow execution protectionsと`pull_request_target`

2026-09-17にgenerally availableになった。Actionsを起動できるactorとeventのallowlistをrepositoryに掛けられ、対象のworkflow fileを絞る適用、insights、REST APIも使える。publicなrepositoryで、適用されるevent policyが無い場合は、`pull_request_target`を止める既定のruleが入る。最初はevaluate modeで動き、2026-11-02に、GA前に既定の`pull_request_target` policyを使っていたrepositoryへ自動で強制される。`pull_request_target`を使うworkflowがあるときは、insightsで影響を確かめ、止めたままにするか、必要なworkflowだけをevent policyで明示的に許可する。privateとinternalのrepositoryには既定ruleは掛からない。

公式資料:

- [GitHub Actions設定](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
- [Actions permissions REST API](https://docs.github.com/en/rest/actions/permissions)
- [Get a repository ruleset](https://docs.github.com/en/rest/repos/rules)
- [GitHub ActionsをDependabotで更新](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions)
- [Workflow execution protectionsのGA（2026-09-17）](https://github.blog/changelog/2026-09-17-workflow-execution-protections-in-github-actions-generally-available)
- [About Actions policies](https://docs.github.com/en/actions/concepts/about-actions-policies)
- [Control workflow execution](https://docs.github.com/en/actions/how-tos/administer/control-workflow-execution)

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
| Immutable releases | 任意 | releaseを配布するrepositoryでは有効化を検討する（推奨候補） | 公開後のassetsとtagを変更できなくなり、release attestationが付く。有効にした後の新しいreleaseだけに効き、既存releaseは再公開しない限りmutableのまま。無効に戻しても、作成済みのimmutable releaseは変わらない |

Dependabot version updatesは`.github/dependabot.yml`で管理する。最低限、利用するpackage ecosystemと`github-actions`を対象にし、更新頻度と同時PR数をrepositoryの保守余力に合わせる。

Private vulnerability reportingは、2026-10-01から次が加わった。報告者ごとの1日あたりの新規報告数に上限が掛かり、repository管理者はrepository全体の1日上限を設定し、信頼する報告者をallow listへ追加できる（Settings → Advanced Security → Private vulnerability reporting）。報告は既定で構造化form（summary、details、150文字以上のproof of concept、impactの4必須）になり、`.github/VULNERABILITY_REPORT.yml`をdefault branchへ置くとformを変更できる。独自formを置くと、REST APIでの提出もそのformに合わせる必要がある。対象は、private vulnerability reportingを有効にしたpublic repositoryである。

Immutable releases（2025-10-28にgenerally available）は、releaseのassetsとtagを公開後に変更できなくし、署名付きのrelease attestationを付ける。repositoryまたはorganizationのSettingsで有効にする。release運用があるrepositoryでは推奨候補だが、公開済みassetsを後から差し替える運用とは両立しないため、運用を確認してから選ぶ。

公式資料:

- [Dependabot version updates](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-version-updates)
- [GitHub ActionsをDependabotで更新](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions)
- [Private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
- [Rate limits for private vulnerability reports（2026-10-01）](https://github.blog/changelog/2026-10-01-rate-limits-for-private-vulnerability-reports)
- [Structured forms for private vulnerability reports（2026-10-01）](https://github.blog/changelog/2026-10-01-structured-forms-for-private-vulnerability-reports)
- [Immutable releases are now generally available（2025-10-28）](https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available)
- [Immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
- [Preventing changes to your releases](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/preventing-changes-to-your-releases)

## 運用profile

### solo / 小規模

- PR、必須CI、thread解決を要求する。
- approval数は0でもよい。人間が最終mergeを実行する。
- squash mergeとbranch自動削除を基本にする。
- auto-mergeはmerge待ちが実害になるまでOFFにする。
- CODEOWNERS と「Require review from Code Owners」は付けない。作者は自分のPRをApproveできず、一人だとマージ不能になる。複数maintainerになってから検討する。
- Copilot code reviewの承認を必須approvalsに数える設定（既定OFF）は、OFFのままにする。数える場合は、人間の最終確認が外れる点を判断記録へ残す。

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
# 上の一覧で得た各IDについて繰り返す
gh api repos/OWNER/REPO/rulesets/{RULESET_ID}
gh api repos/OWNER/REPO/branches/DEFAULT_BRANCH/protection
gh api 'repos/OWNER/REPO/pulls?state=closed&base=DEFAULT_BRANCH&sort=updated&direction=desc&per_page=30'
gh api 'repos/OWNER/REPO/commits/PR_HEAD_SHA/check-runs?per_page=100'
gh api repos/OWNER/REPO/actions/permissions
gh api repos/OWNER/REPO/actions/permissions/workflow
# allowed_actionsがselectedの場合だけ追加取得する
gh api repos/OWNER/REPO/actions/permissions/selected-actions
gh api repos/OWNER/REPO/private-vulnerability-reporting

gh api repos/OWNER/REPO/code-scanning/default-setup
gh api 'repos/OWNER/REPO/code-scanning/analyses?per_page=100'
gh api 'repos/OWNER/REPO/code-scanning/alerts?state=open&per_page=100'
gh api 'repos/OWNER/REPO/dependabot/alerts?state=open&per_page=100'
gh api 'repos/OWNER/REPO/secret-scanning/alerts?state=open&per_page=100'
```

ruleset一覧はsummaryにすぎない。各rulesetのIDから詳細を取得し、conditions、bypass actors、required checks、pull request rulesまで確認する。classic branch protection（`branches/DEFAULT_BRANCH/protection`）も必ず読み、rulesetと累積して評価する。classic protectionが無いbranchは本文`Branch not protected`の404を返す。権限不足は本文`Not Found`の404で区別が付かないため、前者だけを「保護なし」と読み、後者は取得不能として記録する。organization / enterprise policyによる上書きも区別する。

Code scanning alertsは検出結果であり、設定状態そのものではない。alertが0件でもCodeQL設定済みとは判定しない。default setup、`.github/workflows`のadvanced setup、default branchとPRを対象にしたrecent analysesを合わせて確認する。

Actionsの`allowed_actions`が`selected`の場合は、`selected-actions` endpointから`github_owned_allowed`、`verified_allowed`、`patterns_allowed`も取得する。policy modeだけで許可listの妥当性を承認しない。

APIが404や権限errorを返した項目を無効と断定しない。取得不能として記録し、権限、plan、organization policyを別に確認する。

## AIへ読ませて設定する

repo-preflightからは次のread-only intentで、同じ契約のpacketを生成できる。

```bash
python scripts/readiness_scan.py --repo PATH --intent configure_settings \
  --github-settings-profile solo_public --human
```

profileは`solo_public` / `team_public` / `high_risk_public`。このintentは設定変更を実行せず、GitHub APIのGET、比較、previewまでを担当する。

AIへ依頼する場合も、いきなり設定変更を実行させない。次の5段階を固定する。

1. **Inspect**: repository、account、現在値、organization policyをread-onlyで取得する。
2. **Compare**: このガイドの推奨値と比較し、`変更不要 / 推奨変更 / 要判断 / 確認不能`へ分類する。
3. **Preview**: 対象repository、設定名、現在値、変更後、外部影響、rollback、正確な操作を提示する。
4. **Approval**: 現在会話で設定ごとの明示承認を待つ。
5. **Execute / Verify**: 承認された設定だけ変更し、APIで再測定する。未承認項目は変更しない。

Inspectではsummary endpointだけで完了としない。ruleset、CodeQL、selected Actions policyのように一覧・結果・modeと詳細設定が別endpointへ分かれる項目は、詳細まで取得できなければ`確認不能`に分類する。

AIが守る停止線:

- repository名やGitHub accountが一致しなければ停止する。
- visibility、Actions権限、ruleset bypass、security機能、auto-mergeを包括承認へ束ねない。
- organization / enterprise設定をrepository設定と誤認しない。
- 404、権限不足、plan非対応を`false`へ推測しない。
- browser画面の見た目だけで完了とせず、可能ならAPIで再確認する。
- Actions設定のPUTは、実行直前に取り直した現在値へ承認された項目の変更だけを重ねて1回で送る。観測時の値を固定bodyにして順に実行しない。
- 設定変更と`.github/dependabot.yml`などのcode変更を別工程にする。
- push、PR、merge、release、公開、告知を設定承認から推論しない。

### AI向け依頼文

`OWNER/REPO`を実際の対象へ置き換える。

```text
repo-preflight の references/github-settings.md を正本として、
OWNER/REPO のGitHub設定をread-onlyで実測してください。

結果を次へ分類してください。
- 変更不要
- 推奨変更
- 人間判断が必要
- 確認不能

各候補について、現在値、一般的な初期値、推奨値、選択理由、
外部影響、rollback、正確な変更操作を提示してください。
設定変更、push、PR、merge、release、visibility変更はまだ実行しないでください。
私が設定ごとに明示承認したものだけ実行し、直後にAPIで再測定してください。
```

### AI向け機械可読packet

設定候補を次の形で出力させると、現在値と提案を混同しにくい。

```json
{
  "schema_version": "repo-preflight.github-settings-review/v1",
  "repository": "OWNER/REPO",
  "observed_at": "RFC3339",
  "account": "LOGIN",
  "settings": [
    {
      "name": "delete_branch_on_merge",
      "observed_value": false,
      "default_value": false,
      "recommended_value": true,
      "classification": "recommended_change",
      "reason": "短期branch運用",
      "external_effect": "今後mergeしたremote head branchを自動削除",
      "rollback": "設定をfalseへ戻す",
      "approved": false
    }
  ],
  "unknowns": [],
  "external_actions_performed": false
}
```

`approved`はAIが推測で`true`にしない。人間承認後も対象repositoryと現在値を再取得し、staleなpacketなら作り直す。

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

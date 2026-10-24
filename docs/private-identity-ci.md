# 登録済み個人情報の自動検査

この手順は導入候補です。workflow の追加、非公開ポリシーの設定、実際の成功・失敗の確認、必須チェック化は別々の工程です。ファイルを置いただけでは有効になりません。

対象は公開リポジトリです。「非公開」は照合設定を指します。Gitへ認証情報を渡さない取得方式のため、非公開リポジトリ・公開状態不明は取得前に拒否します。非公開リポジトリ用の認証付き取得は別途レビューする対象です。

## 正本を先に取り込む

先に検査器とactionをmainへ取り込み、その後の確定版を利用側へ接続します。このrepo内の [公開workflowテンプレート](../templates/github-actions/public-privacy-preflight.yml) と [非公開照合テンプレート](../templates/github-actions/private-identity-guard.yml) は、配置しただけでは動かない導入用素材です。

`__REPO_PREFLIGHT_MAIN_SHA__` は、実際にmain上に残るレビュー済み完全SHAへ置き換えてから `.github/workflows/` に配置します。未マージの作業branchのSHAを導入版にしません。利用側へ投入する差分と固定先の実体は、マージ・設定適用前に確認します。

## 検査する範囲

公開用のプリフライトに加え、管理者が登録した非公開の照合リストで Git の取得済み全ブランチ・タグ・公開 PR 参照・対象コミットを検査します。削除済みファイルを含む commit/tree/blob/tag のデータ、参照名、作者・コミッター・共同作者などが対象です。信頼済みの後段は公開検査と同じ秘密情報・個人パス検出も独立に実行し、PRが公開検査を空の成功処理へ変更しても検出を免除しません。対象コードは checkout、import、build、実行しません。検査器と照合設定は対象コードから選びません。

既存の文字列復号・正規化で読める登録済み情報が対象です。未登録の個人情報、画像の画素、暗号化、未対応コンテナの意味内容まで不存在を保証しません。取得・検査の上限、設定欠落、破損履歴、読み取りエラーは検査不能として失敗します。基準値ファイルで検出を許可する仕組みはありません。

静的な引用の連結と一文字区切りの復元では、同じliteral文法で閉じquoteへ到達できる開始位置を一度計算します。閉じない引用を各位置から繰り返し探索せず、既存のtoken・quote候補の位置と内容を維持します。これは本文検査の性能修正で、検出対象やbaselineの免除は追加しません。

## 信頼する実行経路

`public-privacy-preflight.yml` の公開プリフライトが終了した後、default branch 上の信頼済み `workflow_run` が、完全 SHA に固定した `.github/actions/private-identity-guard` を実行します。PR の編集可能な workflow へ非公開ポリシーを渡しません。PR の artifact、キャッシュ、スクリプトも実行しません。GitHub API で元の実行と現在の PR head を照合し、そのコミットに `private-identity-guard` check を記録します。古い head の結果で新しい head を成功扱いにしません。

検査結果は専用の GitHub App から発行します。Appの権限は対象repoの `actions: read`、`contents: read`、`pull_requests: read`、`checks: write` と既定のmetadata読取りだけです。書込みは検査結果だけに限定します。通常のGitHub Actions Appと別の発行元を使い、実際のcheckの `app.id` と設定済みのApp IDが一致しなければ失敗します。通常の `GITHUB_TOKEN` へのfallbackはありません。公開ログ・check 本文は成功／失敗だけです。照合値、検出文字列、対象パス、例外の生値は出しません。秘密情報を使う workflow で対象コードを実行しないことは、今後の変更でも維持してください。

## 導入する順番

1. 検査器の候補をレビューし、通常 CI と合成データの失敗試験を通す。
2. 人間レビュー後に専用Appを作成し、まず対象repoだけへinstallする。環境 `privacy-check-trusted` を作り、Selected branchesのexact `main` だけを許可する。tag・`refs/pull/*/merge` の許可を追加しない。管理者の保護迂回も無効化を確認する。「Protected branches only」は未設定時に全branchを許すため使わない。
3. 保護環境を読戻して確認してから、environment Secretsに `PREFLIGHT_CHECK_APP_PRIVATE_KEY` と照合用の `PREFLIGHT_PRIVATE_DENYLIST`（行形式）、`PREFLIGHT_PRIVATE_IDENTITY_POLICY`（既存 `identity_gate` のJSON）を登録する。environment variable `PREFLIGHT_CHECK_APP_ID` は実App ID（client IDとは別）にする。鍵・照合設定をrepo Secretsに置かない。値をIssue・PR・ログに貼らない。
4. 人間の承認後に信頼済みworkflowをdefault branchへ導入する。App/環境/設定が無ければ成功にならない。環境の設定前に鍵や照合値を登録しない。
5. 導入後の新しい PR で、現在の head に結び付いた成功と合成の検出失敗を確認する。fork／Dependabot／merge queueの経路も確認する。同名の通常Actions jobを成功させても、専用Appの失敗を代用できないことを必ず確認する。`workflow_run` は default branch へ入る前には動作しないため、PR 上の通常 CI 成功をこの確認の代わりにしない。
6. 実際に出た `private-identity-guard` をその専用App IDに束縛し、公開プリフライトのcheckとともに、既存の必須チェックを残してmainのrulesetへ追加する。通常Actions App（15368）を専用発行元の代用にしない。実在未確認の check 名を先に必須化しない。

この CI は公開 push **後**に動き、必須チェックは merge を止めます。初回の漏出を抑えるには、送信前のローカル検査と、対応する秘密情報を対象にする GitHub push protection も併用します。ローカル hook は実際の送信元ごとに導入・動作確認が必要です。hook の配置だけで全送信経路を防いだとは扱いません。

## 検査器の更新

公開／非公開の二つの composite action は、それぞれ `uses: nexus-ai-2045/repo-preflight/.github/actions/...@<完全 SHA>` の一か所でソースを選びます。従来の reusable workflow の `uses` と `preflight-ref` の二重指定を、新規接続では使いません。別の公開名義一覧を使う利用側は、公開actionの `allowed-identities` にレビュー済みのtarget内JSONの相対pathを指定できる。絶対path・target外への参照・symlinkでの逸脱は拒否する。非公開側の名義policyも管理者が確認して揃える。

利用側の Dependabot `github-actions` で更新 PR を作り、テストとレビュー後に採用します。自動 PR の作成は無審査の更新適用ではありません。

同梱アーカイブを持つ利用側は、Action の SHA だけでなく、アーカイブ・manifest・wrapper・文書の固定値を同じ版へ揃え、ハッシュを再確認します。既存の保存済みアーカイブを自動削除しません。

## 運用と復旧

検査失敗では、非公開の管理経路で照合設定・取得範囲・リソース上限を調べます。公開ログへ値を増やして原因を調べません。ポリシーを空にする、失敗を無視する、基準値を増やす復旧は行いません。設定を戻す必要がある場合も、人間がレビューした rollback を使い、保護が外れる期間を明示します。

設計の根拠は GitHub の [workflow_run の仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run) と [信頼できないコードを実行しないための指針](https://docs.github.com/en/actions/reference/security/secure-use) です。

forkのPRはbase側のopen PR一覧を全ページ取得してheadを照合する。取得上限では失敗にする。merge queueでは `merge_group` の合成SHAへ結果を結び付ける。

発行元と環境保護の根拠は、[必須チェックの対象と発行元](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)と[環境のbranch制限](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)を参照。設定保存だけで実際のmerge強制を確認したとは扱わない。

名義検査は整形ログの区切り文字に依存せず、Git原本のcommitヘッダーを読みます。制御文字・重複した名義・不正な形式は失敗し、未知の名義を許可済みの値へ置き換えません。commit原本の非UTF-8は検査不能として停止します。

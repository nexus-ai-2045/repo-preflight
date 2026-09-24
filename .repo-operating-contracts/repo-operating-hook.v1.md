# Repo 運用 Hook v1

状態: 配布用
モード: 助言。配置のみでは有効化されない

## 目的

作業開始時と書き込み前に、次の2点を1つの入口で確認する。

1. 作業ディレクトリと Git repository が想定どおりか。
2. 現在の話題が repo goal から分岐し、別task化または人間レビューが必要か。

## 呼び出す判定

- `workdir_git_identity_check.py`: repo root、remote、branch、upstream、ahead / behind、dirty state をread-onlyで確認する。
- `side_route_decider.py`: 継続、別task推奨、人間レビュー停止を判定する。

判定が必要な場合は `side-route-chat-router` skillを発動する。hookは検知入口、skillは判断とユーザー向け説明を担当する。

## 停止線

このファイルをrepoへ配置してもhookをinstallまたはenableしてはならない。現在会話で対象と操作を明示した人間承認があるまで、次を実行しない。

- hook install / automation enablement
- push / PR / release
- repository visibility change
- 外部送信、投稿、告知
- auth、secret、production設定の変更

## 推奨結果

- `continue_here`: repo identityとrepo goalが一致し、安全に継続できる。
- `suggest_new_chat`: repoまたはgoalが分岐しているため、別taskをすすめる。
- `stop_for_review`: 公開・外部操作・hook・authなどの停止線、またはGit同一性の不一致がある。

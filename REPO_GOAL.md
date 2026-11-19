# Repo Goal

repo: repo-preflight
owner: nexus-ai-2045
current_goal: AIがGitHubに非公開リポジトリを作って初めて送信するとき、またはリポジトリを公開するときに、送信・公開される履歴の個人名義、個人パス、秘密情報の混入を事前に検出する。人・AI・CIに検査範囲と停止理由を返し、人が対象と操作を確認してから進める。

## 完了レイヤー

- local implementation:
- local verification:
- branch / commit:
- push / PR:
- merge / external state:
- cleanup:
- unrelated dirty state:

## 停止線

- publication: 現在会話での人間レビューと明示承認まで停止
- repository visibility: repo単位の明示承認まで変更しない
- external send / post / comment: 明示承認まで停止
- hook / automation: 配置のみ。install / enableは明示承認まで停止
- auth / secret / production: 明示承認まで変更しない

## Evidence

- git state:
- changed files:
- verification:
- external actions performed: false
- remaining risks:

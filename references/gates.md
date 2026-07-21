# 承認手順

外部操作は `inspect -> preview -> approval -> execute -> verify` の順にする。

## Fail closed

- owner、account、remote、repo rootが一意でない。
- dirty/untracked/history/tags/stashesの検査範囲が不明。
- secret、個人情報、絶対path、第三者素材の権利が未処理。
- dependency advisory、license、CI workflow securityが未検査またはstale。
- test、build、smoke test、E2E、reviewの必須checkが失敗。
- project登録、PUBLIC_READY、人間目視reviewが現在HEADと一致しない。

## GitHub操作

private repo作成、push、PR、merge、public化、告知を別承認にする。GitHub connectorはPR/issue情報を優先し、branch/commit/push/account/Actions logはlocal git/ghで補う。

## 履歴と名義

working tree設定、未公開commit、既存履歴、署名、GitHub owner、README metadataを別々に測る。履歴書き換え前にcommit数、旧新identity、signed commit/tag失効、force-push可能性を提示する。

## 後片付け

mergeをdefault branchで確認後、他branch/tag/worktreeから参照されず、uncommitted/unpushed workがない場合だけ削除候補を提示する。削除は明示承認後に行う。

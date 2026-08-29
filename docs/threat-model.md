# 脅威モデル

## 保護対象

- 公開前repositoryに含まれるcredential、個人情報、非公開path
- 公開可否判定の完全性とfail-closed性
- 検査対象repositoryとローカル作業環境の完全性
- GitHub owner、作者名義、承認記録の真正性

## 攻撃者入力

- 細工されたファイル名、非ASCII path、symlink、gitlink
- 壊れた、欠落した、または巨大なGit object
- credentialを含むremote URL
- binary、異常encoding、secretに似た文字列
- 悪意あるCI workflow、依存package、第三者素材
- staleなテスト結果、別HEADのreview、包括的な公開承認

## Invariant

- 読み取れない対象やGit probe失敗を`pass`へ丸めない
- secret本文やremote credentialを出力しない
- subprocessへshell文字列を渡さず、固定引数と`cwd`を使用する
- scannerから外部送信、Git変更、visibility変更を行わない
- `unknown`、検査不足、HEAD不一致があれば公開ライフサイクルを停止する
- public化は対象repo固有の現在会話での明示承認なしに行わない

## Failure modeと対策

| Failure mode | 影響 | 対策 |
|---|---|---|
| Git object欠落・異常応答 | 履歴secretの見逃し | `tool_error`でfail closed |
| 読取不能ファイル | 現行treeの見逃し | `worktree_file_unreadable`で停止 |
| 2 MB超blob | 内容未検査 | 制約として明示し、公開前にlarge-file一覧を別確認 |
| regex外secret | false negative | gitleaks等の専門scannerと人間レビューを併用 |
| remote credential | report経由の漏えい | userinfoを除去してURLを返す |
| 古いreview/CI | 別の差分を誤承認 | 対象HEAD、日時、CI runをreview記録へ固定 |
| `GIT_DIR`等による対象差し替え | 別repositoryの判定を`--repo`の結果として返す | git呼び出しから repository 上書き環境を除く |
| dependency/action改ざん | CI上のcode execution | 最小権限、credential非保持、Actions full SHA固定、dependency audit |
| 誤ったGitHub account | owner境界違反 | 操作前にactive accountと`owner/name`を再測定 |

## 残存リスク

scannerは完全なmalware解析、PII分類、license法務判断、GitHub設定監査を行いません。公開前には専門scanner、依存監査、GitHub human review、repo固有承認を別証拠として揃えます。

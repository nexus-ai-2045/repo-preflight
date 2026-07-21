# Repository catalog登録契約

1. repo内のAGENTS、README、既存registry/runbookを検索する。
2. organization側のrepository catalog、project inventory、同名remoteを検索する。正式な登録先がなければ新しい形式を勝手に作らず、登録を停止する。
3. `owner/name`、purpose、local path、remote URL、visibility、lifecycle state、公開identity policy、HEAD、verified_at、evidence、residual workを記録する。
4. owner/nameまたはlocal pathの重複を検出し、既存entryを冪等更新する。競合時は自動統合しない。
5. registry validatorがある場合は実行し、diffとvalidation結果を登録証拠にする。

登録はGitHub repo作成を意味しない。台帳write、remote作成、push、public化を別操作として扱う。

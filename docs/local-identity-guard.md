# 個人名義を公開側に出さない

`scripts/local_identity_guard.py` は、持ち主の個人名義を人に書かせずに照合します。OSのアカウント名・ホーム名・氏名・gitのglobal名義・端末名と、そのrepoの履歴で私用メールを使ったコミットの名義を、実行中の環境から毎回集め、値はメモリ上だけで使います。報告は種類とhashの先頭だけで、値そのものは表示しません。

```sh
# 今のrepoで使われるコミット名義が公開用（GitHubのnoreply）か
python scripts/local_identity_guard.py --public-name <公開用の名義> config
# push前：まだremoteに無いコミットの名義・メッセージ・追加行・ファイル名
python scripts/local_identity_guard.py --public-name <公開用の名義> check --pre-push --remote origin < <pre-pushのstdin>
# 既存のidentity guardに、集めた値を PUBLIC_IDENTITY_DENYLIST_EXTRA で渡し、出力の値を伏せる
python scripts/local_identity_guard.py exec -- <guardのコマンド>
```

`~/.config/repo-preflight/private-identities.txt` があれば追加で読みますが、無くても空でも止めません。

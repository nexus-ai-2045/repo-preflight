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

新規枝の `--pre-push` 検査では、`--remote` に指定した実送信先を `git ls-remote --heads --refs` で一度照会します。remote 名は Git 自身の `remote get-url --push --all` で単一の push URL に解決し、`pushInsteadOf` / `insteadOf` を反映します。fetch URL と push URL が異なる場合も push 側を検査します。複数の push URL がある場合は送信先を特定できないため停止します。

URL・ローカルパス直指定では、`pushInsteadOf` の設定が一つでもあれば、無関係な宛先の設定であっても安全側へ停止します。設定の読取り失敗も停止します。設定が無い場合だけ `git ls-remote --get-url` で `insteadOf` を解決します。書換え規則の独自実装や、一時・永続の remote 設定変更は行いません。

解決した push URL を `git ls-remote --get-url` で通信前に確認し、照会時の `insteadOf` などで別の宛先へ再書換えされる場合は停止します。異なる送信先の履歴を除外根拠にしません。

除外するのは、その時点で送信先の枝から参照され、ローカルにも commit として存在する履歴だけです。古い remote-tracking ref、別の送信先の履歴、未取得の先端は除外根拠にしません。空の送信先やローカルで読める先端が無い場合は全履歴を検査します。新しいコミットの名義・メッセージ・追加行・ファイル名の検査は維持します。

照会の失敗、不正な応答、15秒の時間切れでは検査を停止し、URL・Gitのエラー本文は表示しません。履歴を取得する fetch や設定変更は行いません。既存枝の更新は従来どおり旧先端から新先端の範囲を検査し、枝の削除は検査対象から外します。この照会は実行時点の情報であり、その後の送信先の変更までは保証しません。

`exec` は今回自動取得した値に加えて、親から `PUBLIC_IDENTITY_DENYLIST_EXTRA` で継承し子へ転送した値も、出力の伏せ字対象に含めます。継承値もメモリ上だけで扱い、子の標準出力・標準エラーの両方を伏せます。

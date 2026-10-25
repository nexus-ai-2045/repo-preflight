# ADR-0011: 送信に含まれない既存JPEGの構造不正は送信全体を止めない

- status: Accepted
- date: 2026-10-23
- amended: 2026-10-26（明示private送信scope）

## 問題と選択

[ADR-0010](0010-strict-jpeg-private-scan.md)のstrict復号は維持する。一方、比較元より前からある既存JPEGを復号できないとき、その失敗で今回の送信全体を止めると、画像を含まない変更も出せなくなる。

個人識別子の作業tree在庫は全体のままにする。差分だけを見る案は、未commitの個人情報や、差分外の読めるfileを見逃すため退ける。

採用するのは次の境界だけである。

- 今回の送信は、比較元からHEADまでの差分と、作業treeの未commit追加・変更である。
- 送信に含まれるJPEGの構造不正、依存不足、警告、上限超過は走査全体を止める。
- 送信に含まれない既存JPEGの `private_identity_jpeg_unverified` は、そのfileを未展開として数え、走査は続ける。
- JPEGとPNGの容量上限は本文照合より前に出る。未展開にはせず、従来の `tool_error` で止める。
- 読めるfileのliteral・絶対pathは在庫全体で止め続ける。PNGの検査不能は止め続ける。
- 構造不正の既存JPEGに含まれる文字を、復号失敗後に別経路で読むことは保証しない。

## 見直す条件

既存JPEGの未展開を、登録済み個人情報の見逃しとして運用で観測した場合は、この限定を外してADR-0010の停止へ戻す。

## 明示private送信scopeの改訂（2026-10-26）

通常のPRで送るのはコミット済みのGitオブジェクトであり、未commit・未追跡の本文は送信対象ではない。上の「今回の送信」に未commit変更を含めた定義は、明示した `--private-submission --base-ref origin/<branch>` に限り、次の定義で置き換える。既定の検査・公開・releaseにはこの限定を適用しない。

- CLIでは `push` / `open_pr` / `merge` のintentだけに使える。releaseとの併用、比較元なしでは拒否する。
- GitHub APIでoriginが `private=true` かつ `visibility=private` と確認でき、live base branchのSHAとローカルremote baseが完全一致し、baseがHEADの祖先である場合だけ使える。取得不能・public・不明・不一致は検査不能で止める。
- 実効push URLはcredentialを含まない同じGitHub repositoryへ向く単一URLでなければならない。
- `base..HEAD` に導入されたcommit・tree・blobをGitの固定OIDとraw bytesから検査する。中間commitで追加後に削除した本文、commit metadata、変更entryの名前、同一blobの改名も対象にする。treeのraw構造・OIDは検証するが、変更していない兄弟entryの名前を再判定しない。
- 作業treeの内容検査は実行しない。未送信のdirty・未追跡本文や変更していないbaseline blobは復号しない。既存のclean-worktree・整合性gateは維持し、dirtyだから通過するとは限らない。
- 結果は `private_committed_submission` とbase/head SHA、live private確認を記録する。repository全体を検査した証明にはしない。Git参照・GitHub状態が変わった後の送信保証でもない。

対象になった画像の検査不能は従来どおり止める。既存の形式・容量制限、履歴走査の上限、キャッシュ方針は変更しない。既定の `--base-ref` だけの場合は上記の在庫検査を保ち、full・公開・releaseはrepository全体の検査を維持する。

改名などでPDF・MP3・MP4の形式宣言が追加・変更されたcommitted tree entryは、blobがbaselineに既存でも再検査します。変更していないbaseline全体は追加しません。

明示private送信では、同じpathの直前の比較親と現在commitから得たtextだけを比較します。変更text内の不変行と不変の正規化候補は再判定せず、追加本文と新しい正規化値を調べます。変更部分に依存する再構成は、同値が親の別箇所にもあっても検査します。正規表現の役割に基づく既存のmaskは保持します。新規・copy・renameで同pathの比較元が無い場合、binary・media、commit metadataは全文検査のままです。全中間commitを調べるため、途中で追加後に削除した値も対象です。外部providerで差分の同等性を証明できない場合は全文側へ倒します。

text比較は合計200万文字またはold/new行数の積1600万を上限にし、超えた変更fileだけを全文検査します。`private_identity_scan.text_delta_full_fallbacks` がその回数を記録します。不変性を証明できない候補も検査側へ倒します。公開・fullの検査方針、画像・媒体のstrict decoderと既存上限は変更しません。

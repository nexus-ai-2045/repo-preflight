# セキュリティポリシー

## 報告

脆弱性の疑いを、確認前に公開issueへ投稿しないでください。GitHub Security Advisoriesのprivate vulnerability reportを使用してください。この経路を利用できない場合は、機密情報を公開せず、repository ownerへ非公開報告経路の案内を求めてください。

報告には、影響範囲、再現手順、想定される悪用方法を含めてください。secret、個人情報、第三者の非公開データは必要最小限にし、公開issueやpull requestへ添付しないでください。

## 対象

最新の`main`をサポート対象とします。scannerの検出はヒューリスティックであり、専門scannerや人間レビューを置き換えません。

## データ

検査結果にsecret本文を出さず、候補種別とファイル位置だけを扱います。実credential、private conversation、個人情報をfixtureへ入れないでください。

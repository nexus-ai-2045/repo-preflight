# ADR-0012: 画像の検証済みbinary領域とGit相対名を区別する

- status: Accepted
- date: 2026-10-24
- adoption: 2026-10-24、本人がリンクされた本提案とpixel/codingに隠した文字列が検出対象外となるリスクを確認し、明示採用。採用記録は準備済み提案patch `bebba547b5ae151737684d970a7314ebdc1098d417c729433cf195b7785ba7c9` に束縛する。
- rebuild provenance: 元の採用実装 `719dc3ac97c485948da7dc94636728b4cb293654` は再構築後の履歴に含まれないため、2026-10-24に現行main `ad1531f` へ最小移植する。本人採用は上の記録を引き継ぎ、PSD/GIF・公開名義一覧・その他形式の検査契約は維持する。
- changes: ADR-0010の全byte literal照合契約を明示的に変更する。通常の不具合修正として扱わない。

## 背景と採用判断

全byteを文字として検査すると、正しく復号できる画素や数値表のbyte列が、偶然policyの文字列と一致する。現在の検査はその一致も拒否する意図的な契約であり、この採用判断はそれを変更する。

既存PNG parserとJPEG marker parser・strict decoderを使い、構造と復号の検査が完了したcoding・pixel領域をbinaryとして扱う。新しい画像decoder、除外policy、baseline免除、画像の再encodingは追加しない。PNGの画像データと文字metadataの区別は[W3C PNG仕様のIDAT](https://www.w3.org/TR/png-3/#11IDAT)と[文字情報](https://www.w3.org/TR/png-3/#11textinfo)を参照する。

| 領域 | 変更前 | 採用契約 |
|---|---|---|
| PNG IHDR・PLTE・IDAT・IENDとCRC | raw文字列一致も拒否 | 既存の構造・CRC・展開・scanline検査後、文字照合の対象外 |
| PNG tEXt・zTXt・iTXt | raw照合と復元済み文字検査 | 維持。付加payloadのUTF-8・Latin-1・UTF-16 LE/BE両alignment、URL表記も確認 |
| PNG未知付加chunk | raw照合・文字列の静的復元 | 維持。未対応critical chunk・APNGは検査不能 |
| JPEG entropy・SOF・DQT・DHT・SOS・DRI | raw文字列一致も拒否 | 既存のstrict復号成功後、文字照合の対象外 |
| JPEG検証済みJFIF固定項目・thumbnailとAdobe数値項目 | raw文字列一致も拒否 | 構造・長さ検査後、binaryとして文字照合の対象外 |
| JPEG COM・APP1 XMP本文 | raw・URL・UTF-16・静的復元 | 維持 |
| その他JPEG APP payload（Exif等） | raw decoded・URL・UTF-16によるliteral/path照合。source式としては解析しない | 維持。すべてのAPPを静的sourceとして復元するとは主張しない |

chunk/segmentの境界、metadata上限、画像上限、CRC不正、破損した圧縮データ、欠損、decoder不在、warning、末尾追記は現在と同じ停止経路へ送る。metadataのpathをreviewed raw pathで免除することも認めない。

## Gitの相対名

本文の絶対path文法は変更しない。Git在庫の相対pathと末尾suffix除去名だけを、同梱providerの`scan_private_repository_name`へ明示的に渡す。保守的なcomponent構文に合う相対名では、groupやparameter componentの後の`home` componentをhostの絶対pathと解釈しない。

名前に含まれるdenylist literalは維持する。先頭slash、空白、colon、backslash、URI、quote、不完全group、CLI option等はこの相対名扱いに入れず、既存検査へ戻す。外部providerに新APIがない場合も既存検査を維持する。記事本文・metadata・任意の文字列へこの名前専用contextを適用しない。

## 回帰と変わる保証

合成画像のpixel/codingにだけliteralを置くケースを、旧契約のfailから採用契約のpassへ明示変更する。metadata literal・静的復元・UTF-16の奇数offset、未知付加chunk、末尾literal、壊れたCRC/scanline、strict復号失敗は引き続き拒否する。実Git在庫のgroup route名と、同じsource内の絶対pathを分けて検証する。private画像・実policy値はfixtureに含めない。

この契約では、検証済みpixelや数値表に意図的に埋め込んだ文字列も検出対象から外れる。OCR・steganography・coding表を利用した隠蔽の検出を保証しない。画像全byteのliteral不存在を要件とする利用者には不適合であり、本人がこの制約を確認して採用した。metadataの新しいviewは既存の処理上限を消費し、検査不能なら停止する。

この契約変更・更新した旧期待値・独立レビューを伴うPRを正規mainへ取り込み、その確定版を利用側で固定する。ローカル検証やPRの成功だけで、利用側のpreflightも成功したとは報告しない。

# ADR-0010: JPEGの復号を検証してから画素をコード解析と分ける

- status: Accepted
- date: 2026-10-22

## 問題と選択

PNG対応後、拡張子がPNGでも実体がJPEGの既存資料で同じ静的文字列解析停止が見つかった。拡張子や画像ディレクトリ全体の除外は採用しない。marker構造だけの自作検証も、画像復号を完了した証拠にはならない。

Pillowの通常の読み込みは、異常な圧縮データを補修して成功する場合があるため、拒否ケースの検証には使わない。[simplejpeg](https://github.com/jfolz/simplejpeg)の`strict=True`を採用し、修復可能なエラーも拒否する。Pillowは合成テスト画像を作るtest依存だけにする。画像の復号を新たに自作しない。

## 境界と運用

- JPEGの実signatureで判定する。元の画像・履歴は変更しない。
- 全byteのliteral・絶対path照合を保持する。COMのような文字metadataのsegmentには既存の静的文字列連結・URL表記・文字検査を適用し、scan間や末尾も確認する。未解析のAPP0–APP15 payloadは文字metadataと扱わず、source連結は適用しない。raw照合は維持する。APP1のXMP本文だけは文字metadataとして連結検査へ渡す。strict復号で確認するDQT/DHTなどの符号化表・画像headerはsource codeと解釈しない。JFIFの固定項目・サムネイルとAdobeの数値項目も構造を確認して分け、全byteのraw照合は維持する。
- 復号は隔離したPython processへ渡す。標準入力の画像をコードとして実行せず、入力・出力画像・metadataの量と復号時間を制限する。失敗、警告、依存不足は安全な診断コードで停止する。
- metadata由来のpathはraw path例外で免除しない。元のtext検査、secret検査、commit範囲を維持する。
- JPEGを含むCLI runtimeには任意の`images`追加依存を導入した仮想環境を使う。system Pythonやhomeのhook設定を自動で変更しない。ライブラリがない実行経路を成功扱いにしない。
- OCRや画素内の隠蔽情報の検出を保証するものではない。未対応のJPEG形式・解釈不能なsegmentは停止を維持する。

## 局所検証

通常／progressive JPEG、各scan位置のmetadata、文字列連結・URL・UTF-16、raw path例外、構造不正と復号不正、デコーダー不足・警告・timeout・上限を合成fixtureで確認する。実資料はPRへ含めず、検査が止まった同一bytesをローカルで再照合する。

## 関連

送信に含まれない既存JPEGの検査不能が走査全体を止める範囲は、[ADR-0011](0011-existing-jpeg-unverified-does-not-block-submission.md)が限定する。strict復号と、送信に含まれるJPEGの停止はこのADRのまま。

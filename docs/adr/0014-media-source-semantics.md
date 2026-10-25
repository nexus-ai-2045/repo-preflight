# ADR-0014: PDF・MP3・MP4を形式decoderで確認してから文字情報を検査する

- status: Accepted
- date: 2026-10-24
- adoption: 本人が「形式別検査を採用して進める」を明示選択。画像だけの採用を他形式へ転用しない。
- source_proposal_sha256: 0972937db407fa7e31874cc30d2a8f81ef58f8d2021306eaeb717826cd379ff9
- scope: 原本bytes保持、正常復号された本文・metadataを検査し、未知・不正・読取不能を拒否する。

## 判断と責務

PDF・音声・映像の符号列をそのままsource codeや文字列と解釈すると、内容と関係のないbyteの一致を検出する。既存の形式decoderが正常に読める場合に限り、本文・metadata・字幕等を既存identity検査へ渡す。圧縮符号列へ隠した文字列、画面や音声そのものの認識、OCR・音声認識・steganographyは保証しない。原本の再圧縮、metadataの削除、unknownをpassへ変える処理はしない。

`media_identity.py`は抽出と形式確認だけを所有し、`scan_private_media(data, patterns, *, kind, check_text, check_image)`へ渡された既存callbackがidentity・pathを判定する。別のdenylist、名義判断、台帳、収集器は作らない。対応する`kind`は`pdf`・`mp3`・限定的な`mp4`。復号されたmetadataは既存identity/path callbackへ渡し、readinessの既存SECRET_PATTERNSも同じ文字viewを検査してsecret_scanへ別集計する。戻り値は既存status/count/path/reconstructed項目にadapter SHAとdecoder版を付ける。countは複数の抽出箇所から返されたcallback件数の合計で、旧全byte検査の件数との一致は意味しない。

## 既存parserの利用

- PDFは[pypdfのstrict reader](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html)でxrefの通常・object-stream内の文字object、metadata、本文を読む。暗号化、実行action・添付、外部stream、未知streamは拒否する。過去revision・hybrid・linearizedの全linked xref componentは同一原本の既存readerで確認する。URI/GoToの既知link actionだけを読取り対象とし、後続actionも配列を含めて検査する。URLを取得しない。
- PDFのcontentは[pypdfの本文抽出](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)と既存PDF操作parserを使う。埋込みTTF/OpenType・CFFはfontToolsでtable・outlineを読み、font名等の文字metadataも検査する。未対応font/任意table、未検証Type1 variant、SVG・bitmap font、DSIG等は拒否する。画像XObjectは復号を確認し、対応する元JPEGと抽出画像を既存image callbackへ渡す。JPX・inline image等、元のimage metadataを確認できないものは拒否する。
- MP3は[Mutagen ID3](https://mutagen.readthedocs.io/en/latest/api/id3_frames.html)でtext・URL・歌詞・coverを読む。ID3v1もv2とは別に確認し、APE textも読む。未知frame、private binary/任意attachment、読めないmetadataは拒否する。paddingとtrailerを黙って除外しない。
- MP3音声は[ffprobe](https://ffmpeg.org/ffprobe.html)とFFmpegで全対象streamを復号する。decoderが報告したpacketのbyte範囲とMutagenの既存領域情報を用い、未知trailerを検査済み符号列と扱わない。coverは元の画像metadataを既存image callbackで検査し、未知diagnosticやdecode errorは停止する。

## 限定MP4と構造領域の責務

MP4はHachoir3.4.0（GNU GPL v2、optional依存・非vendored）のstrict field traversalを既存隔離CLIで使い、未解釈構造を圧縮映像と誤認しない。対応はH264 video streamが正確に1本だけで、audio・字幕・cover/attached picture・他codec/streamは停止する。

全入力をFFmpeg/ffprobeのmov demuxerへ固定し、enable_drefs=0・use_absolute_path=0・protocol=file,pipeで外部trackを開く前に境界を設定する。同じstaged sourceのhashへ、packet範囲・全frameのxerror decode・成功したtrace_headers・Hachoir CLI結果を結合し、処理後もsource/helper hashを再確認する。helperは固定SHAの実ファイルをpython -Iで呼ぶ。

mdatの正確なdecoder packet範囲だけを映像符号列として扱う。containerの補集合は元offset/bytesと照合してすべて文字viewへ渡す。H264 SEI user_data_unregisteredのUUID16bytesとpayload、同messageの連結もUTF8/Latin1/UTF16両endian・両alignmentで検査する。未対応SEI/NAL/codecは停止し、抽出成功をidentity成功と扱わない。metadata範囲へ結合したAVCC4byte diagnosticはその全bytesも検査し、一般的なdecoder warningは免除しない。既知のHachoir reserved-field/fourcc診断は固定code・offset・sizeだけを返す。

## 入力・隔離・上限

optional `media`依存はpypdf 6.21.2、pdfminer.six 20260107、Mutagen 1.48、fontTools 4.67.0、Hachoir3.4.0、Pillow。FFmpeg/ffprobe executableは利用者が用意する。依存が無ければ固定診断codeで停止し、ネットワークへのfallbackやhome/systemへの自動導入はしない。

短命の隔離Python processと専用temporary directoryを使う。raw credentials・Python検索pathを子へ渡さず、shellやPDFのJavaScript/actionを実行しない。Type1のfont programは、既存fontToolsの形式decoder内で命令・stack・allocation等を制限して解釈し、host file/network I/Oを許可しない。FFmpegのprotocolをfile/pipe、demuxerを対象形式へ限定し、外部data referenceを有効化しない。元bytesと処理後temporary inputのhash一致も確認し、抽出内容・raw decoder stderrを結果や公開logへ出さない。

入力64MB、抽出文字合計2MB、抽出画像合計32MB、画像数256、page1000、object/packet100000、stream32、duration1200秒、各decoder時間と総抽出時間・出力の上限を持つ。PDFのstream展開合計・page content・image寸法、FFmpegの単一allocationにも上限を設ける。POSIXではCPU/file-size上限、Linuxでは追加のaddress-space上限を使う。Darwin/WindowsにはLinuxと同じhard address-space保証をしない。library内部の一時allocationまで全OSで同じ上限を証明するものではない。

adapter・callback normalizer・policyだけでなくdecoder版/利用可否もcacheの前提である。lookup前にその環境を既存cache contextへ束縛できない形式は、clean-pass cache再用を行わない。metadataだけでcache前提の検査を済ませたとは扱わない。

## 検証と限界

公開fixtureは合成PDF・短い音声/映像・cover/字幕だけとする。正常本文、隠れた文字object、font metadata、ID3v1/v2、coverと、暗号化・action配列・添付・未知領域・破損・上限・dependency不在・timeout・資格情報非継承を別々に確認する。限定H264の正常受入、container/SEIの文字検出、unsupported stream・不正IPC・source/hash差・依存欠如・process失敗を別々に確認する。実資料をrepositoryへ転載しない。

これは対応する既知の部分集合の契約であり、すべてのPDF・音楽・movieの受入を保証しない。未対応の形式・encoding・font・subtitle、上限を超える版数・object・文字量等はunknown/検査不能として停止する。本文抽出は視覚上の全文字を証明せず、見た目・音声の情報は別の判断である。採用宣言・局所テスト・利用側での実readback・CI・公開承認を同一視しない。

## PDFの原本領域と全xref context

PDFはPython 3.10以上、pypdf 6.21.2とpdfminer.six 20260107（MIT）をoptional依存とする。core Python 3.9は維持し、3.9のPDF検査はdependency unavailableで停止する。内部stream読取り・primitive decoder・tokenizerのsource hashも束縛し、版や実装が違えば停止する。

ASCII85DecodeとFlateDecodeの順のfilter chainは、Python標準のASCII85復号とzlibを使い、各段の全消費と展開上限を確認してから既存pypdfへ渡す。PDFの空白以外を捨てず、ASCII85の終端欠落・終端後のデータ・不正なgroup・Flate終端後のデータは拒否する。元PDFを変換・再保存せず、展開された本文・metadataのidentity検査を維持する。他の未証明filter chainへ一般化しない。

pypdfが原本streamから読んだpstart・declared length・read bytes・data dictionaryを、その初期化済StreamObject identityへ結ぶ。補正・fallback・別memory stream・不整合・重なりは拒否し、同一payloadを持つ別objectも別offsetとして扱う。検証済みの圧縮区間だけraw照合から外す。Flateは標準zlibのeof・unused/unconsumed bytesで完全消費を確認する。未証明filter chainは停止する。XRef streamは既存readerのentry decoderを使い、W3幅・Size・Indexの型/範囲/非重複・exact decoded lengthとFlate全消費を確認する。余剰・不足・unknown entryは拒否する。

過去版は既存readerのstartxref selectorだけを変え、同一原本I/Oへ全linked component（最大64）を結ぶ。Prev/XRefStmの位置・edge・cycleを検査し、訂正/rebuildは拒否する。現在版の完全Root/pagesを必須とし、Rootを持たないlinked partial componentを独立した完全PDFとは扱わない。全componentの全object・metadata・streamを所有readerの文脈ごとに検査する。物理rangeの一致を理由に古い版の意味検査を省かない。最終raw補集合だけexact source read/range/bytesの重複を統合し、partial overlap・bytes不一致は拒否する。object/page/decoded/read/timeの上限は全context合算する。

raw補集合にはcomments・header・free objectも残し、既存の文字・path callbackへ全byte viewを渡す。既存pdfminer tokenizerは位置の発見だけを担当し、PDF文字列・hex・nameはその元位置から既存pypdf primitive decoderで読み直す。pdfminerのhex値やSTRICT設定だけは信用しない。途中EOF・不正primitive・token/時間/容量超過は拒否する。未圧縮binary font/imageもraw照合を免除しない一方、検証済binary streamへPDF字句構文を誤適用しない。decoded content・CMap・ObjStmの文字領域は同じ既存primitiveで確認する。

OpenActionのGoToはpypdfのdestination検証へ通し、StructElemの属性/Aと実Type3 CharProcsのglyph/Aを実行actionと誤認しない。glyphは既存ContentStreamとして解析し、文字列・commentを検査する。Launch・JavaScript・添付等の拒否は維持する。明示known-free参照は以下の標準null互換だけを使い、その他をstrict=Falseやwarning抑止で通さない。

## 有効文書の限定的な互換

[Adobe PDF 1.4 §3.2.9](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.4.pdf)と[pypdf issue325](https://github.com/py-pdf/pypdf/issues/325)にあるnull参照の意味に沿い、正確なgeneration/idがxref_free_entryでfreeと明示され、同一live xref・object streamが存在しない場合にだけ既存NullObjectへ解決する。元のfree object bytesは引き続き全領域検査する。unknown・generation違い・live/free競合・object stream競合・必須辞書の欠落は拒否する。reader実装もsource hashへ束縛し、適用回数を数値で返す。

mailto URIは非空のmail targetとpercent escapeを既存urllibで確認し、原文と通常のpercent復号文字列を検査する。URIは実行・接続・送信しない。空・不正mailto、file/javascript等の未対応scheme、Launch/JavaScript actionは拒否を維持する。

文字列のunique件数上限だけ既存OBJECT_LIMIT=100000へ整合する。測定されたCJK glyph名は約198KBでも旧20000件上限へ達していた。TEXT_LIMIT=2000000や入力・object・時間・出力・memoryの上限は変えず、件数とbyteの上限を別々に検査する。

## font・graphicsの実データrole

実4小型FontFileのguard下readbackで、既存fontTools4.67.0のT1Font・PS tokenizer/interpreter・eexec・CharString decoderが成功した範囲だけType1を扱う。原本Length1/2/3の合計、既存tokenizerのeexec開始、宣言されたcipher範囲、readstringが返したPS string objectと全CharStrings/Subrsのidentityを結合する。未対応hex/variant・未結合read・部分重複・metadata drift・不完全codewordは拒否する。byte列検索によるbinary免除はしない。

既存interpreterの全operation/procedure/loopを予算化し、array/string/dictは割当前、stack/depth、output、外部I/Oもguardする。Ghostscript・system PostScriptは使わない。raw header/trailerと全decrypted metadata/comments/unused source、全font dictionary/glyph名を検査する。binary CharString/Subr区間だけを、正確な既存readの範囲と完全decompile/drawの成功後にmetadata字句解析から外す。IVは標準の宣言長に束縛する。未採用font programへ一般化しない。

通常のPatternType1は既存PDF ContentStream、FunctionType4は既存primitive tokenizerでinert sourceとして確認し、raw/comment/escaped stringを検査する。実行・renderや未知binaryのtext扱いは行わない。

読み取れるfontのchecksum不一致は[TTFontの公式checkChecksums=1](https://fonttools.readthedocs.io/en/latest/ttLib/ttFont.html)の固定diagnosticとして数える。全table・glyph・metadataのdecompileを`ignoreDecompileErrors=False`で必須とし、checksum以外のwarning、未知opaque tableや破損は拒否する。元fontのchecksum整合性を保証したとは報告しない。頼るfontTools実装もsource hashへ束縛する。

Type1の採用範囲は実証したsmall-program subsetとし、既存lexerが巨大literal/commentを生成する前にprogram全体を既存TEXT_LIMITと同じ2MBへ制限する。PDF全体の入力64MBや他形式の上限は変更しない。より大きいType1は未対応として停止する。

埋込みfontの固定fontTools4.67.0は配布metadataのRequires-Pythonに従いPython3.11+で導入する。Python3.10のPDF字句・原本検査は維持し、font decoderがない入力はdependency unavailableで停止する。core3.9やoptional依存不足を検査成功とは扱わない。

# ADR-0019: coreを維持しPillow媒体機能を安全な実行環境へ限定する

状態: Proposed。2026-10-29。本人が採用した互換境界の実装候補。独立レビューと本線反映は別途行う。

## 問題と根拠

従来の依存宣言は古いPython向けにPillow 11を残し、既存環境へ入っている古いdecoderも実使用前に拒否していなかった。既知の依存修復を宣言だけに反映しても、手元のunsafe decoderで入力を読む経路が残る。

[Pillow 13公式metadata](https://pypi.org/pypi/Pillow/13.0.0/json)のRequires-Pythonは3.11以上。[公式13リリースノート](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)と本人の依存修復判断に従い、Pillowを必要としないcoreと媒体decoderの対応範囲を分ける。

## 判断

- coreのrequires-pythonは3.9以上、依存なしを維持する。text・標準PNGはPillowをimportしない。
- Pillowを使う媒体機能はPython 3.11以上かつPillow 13の安定版（>=13,<14）に限定する。media/images/testのoptional宣言とguardの固定に同じ境界を使う。旧11固定blockのみ削除し、13の既存hashは変えない。
- 既存共有normalizerのlazy helperでPython版、PILの宣言版、配布metadataの版を照合する。旧版・不明版・欠損・版の不一致は既存のdecoder/dependency unavailableへ閉じる。GIF/PSD、媒体画像metadata、PDF画像入口で、Image.open・PSD.read等の入力処理前に検査する。
- module import時に媒体依存を要求しない。package importとCLI script importの両経路を維持する。readiness側の設定閲覧は入力をデコードしないため変更しない。
- Python 3.9のtest extraからunsafe Pillowを除き、媒体依存のない環境はoptional媒体テストを欠損skipにする。unsafe版を拒否する負例は媒体依存がなくても実行し、coreテストと区別する。

## 移行と保証境界

既存のPillow 11/12を使う媒体検査は以後合格しない。利用者は対応PythonとPillow 13の媒体用環境を明示的に選ぶ。ツールが環境やhome配布を勝手に変更したり、旧decoderへfallbackしたりはしない。

旧版を実導入・downgradeせず、versionとdecoder入口のmockで11/12/unknownの拒否と入力未読を確認する。現在の13環境で正常系を確認し、coreの旧Python境界はlazy動作の回帰と既存CIで検証する。ローカルで未実行のOS/Python実機を確認済みとしない。Dependabotの警告終了・外部リリースは本線採用と別の状態である。

# コントリビューション

## セットアップ

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

## 開発ルール

- scannerの既定をread-onlyに保つ。
- `unknown`やtool failureをpassへ丸めない。
- secret本文をtest outputやreportへ残さない。
- Windows、macOS、Linuxのpathを考慮する。
- 挙動変更は失敗するtestを先に追加する。
- public化、push、PR、mergeをscannerへ実装しない。

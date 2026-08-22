# コントリビューション

## セットアップ

利用（検査だけ）に追加パッケージは不要です。開発・テストでは次を入れます。

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

## 開発ルール

- scannerの既定をread-onlyに保つ。
- `unknown`やtool failureをpassへ丸めない。
- secret本文をtest outputやreportへ残さない。
- Windows、macOS、Linuxのpathを考慮する（CIは ubuntu/macOS で 3.11+3.13、Windows は 3.13 のみ）。
- 挙動変更は失敗するtestを先に追加する。
- public化、push、PR、mergeをscannerへ実装しない。
- 脆弱性報告は [SECURITY.md](SECURITY.md)、参加の約束は [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) を参照する。

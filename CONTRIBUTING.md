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


## PRを出す前

[`docs/pr-self-review.md`](docs/pr-self-review.md) の20項目を差分に当てる。
上の開発ルールを、実際のレビュー指摘から一般化した停止条件へ展開したもの。

この file は外部で生成された配布物で、手で編集しない。編集すると
`documentation-contract` の `PR self-review copy integrity` が本文hashの
不一致で落ちる。


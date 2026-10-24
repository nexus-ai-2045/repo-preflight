# 検査用wheelの保全

`ai_ratchet_gate-0.1.1-py3-none-any.whl`は、移行前の`nexus-ai-2045/ai-ratchet-gate`のv0.1.1 releaseから保全した原本です。再ビルド・改変していません。新しい同名repositoryのreleaseや、現在のmainから生成したものとは区別します。

- 原配布元：`nexus-ai-2045/ai-ratchet-gate`、v0.1.1、同名wheel。
- サイズ：10,108 bytes。
- SHA-256：`b3f22ab772699d57906326e42189e0ab7a0ee9e33dcad55a90909ece35106cd7`。
- `ai-ratchet-gate.LICENSE`は、このwheel内のLICENSEを変更せず複製したものです。

`requirements-tools.txt`と専用CIは、同梱した同一wheelを固定hashで検査してからインストールします。別のprivate repoへのcredentialやreleaseの作成は不要です。Python本体やテスト依存全体をオフライン提供するものではありません。

更新は、新しい上流artifact・ライセンス・出典・hashをレビューした変更として行います。実装の編集元は上流repoのままで、このwheelを独立に編集しません。hash不一致は停止させ、基準値や検査設定で免除しません。

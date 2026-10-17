# ADR-0009: 公開を専用コマンドへ寄せ、CI はラチェットで止める

- status: Proposed
- date: 2026-10-17

## 背景

repo-preflight は検査するだけで、公開範囲の切り替えは人が別に行っていた。GitHub には private → public の切り替えを条件付きで止める設定が無く、検査を通さずに公開できてしまう。各 repository の CI に検査を入れても、公開操作そのものは CI を経由しない。

## 判断

- `scripts/publish_gate.py` を公開の唯一の入口とする。全体検査が `pass`、手元と origin の全 ref が一致、`--apply` と対象名の打ち直し（`--confirm`）がそろったときだけ `gh repo edit --visibility public` を実行する。既定は dry-run。
- 公開の判定はラチェットの基準値を使わない。基準値は CI を止めないための猶予で、公開してよい根拠にはならない。
- `.github/workflows/preflight-gate.yml` を reusable workflow とし、各 repository は `uses:` 1行で呼ぶ。必須チェックにできる job 名を固定する。
- CI の判定は `scripts/preflight_ratchet.py` で、検査ごとの件数を基準値と比べる。増加、PR での基準値の緩和、締め忘れを失敗にし、`tool_error` は基準値で見逃さない。

## 退けた選択肢

- **CI ゲートだけ**: 公開操作が素通りになる。
- **CI からの自動公開**: 公開の承認が人から外れる。
- **基準値の自動 commit**: CI に書き込み権限が要る。締めるのは PR の作者が `--update-baseline` で行う。
- **指摘の中身で比べるラチェット**: secret の検出値を保存しない方針と両立しない。件数で比べ、入れ替わりを見逃し得ることを文書に書く。

## 境界

- 公開の承認は人が出す。`--apply --confirm` を打つのは人の判断の記録であり、このコマンドの `pass` は承認ではない。
- GitHub の設定画面や `gh` の直接操作による公開は止められない。運用ルールで公開コマンドに限る。
- 読むだけの検査本体（`readiness_scan.py`）の性質は変えない。外部へ影響する操作は `publish_gate.py --apply` だけに置く。

## 見直す条件

GitHub が公開範囲の変更を ruleset や organization policy で条件付きにできるようになったら、運用ルールを機械の制限へ置き換える。

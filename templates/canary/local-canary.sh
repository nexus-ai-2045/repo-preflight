#!/bin/sh
# 機械のカナリア：ダミーの一時リポから一時の bare リポへ push し、止まるべきものが止まるかを見る。
# 値はすべて仮のもの。GitHub には何も送らない。全体の core.hooksPath のフックがそのまま使われる。
# 使い方: sh local-canary.sh [記録ファイル] [機械の呼び名]   出力: 日時  呼び名  項目  結果（止まった/通った）
LOG="${1:-}"
NAME=nexus_ai
MAIL=273569186+nexus-ai-2045@users.noreply.github.com
SID="00000000-0000-0000-0000-000000000000"
HOST="${2:-機械}"  # 端末名は個人名を含みうるので使わない。箱／MacAir／Windows などの呼び名を渡す。
fail=0
one() {
  label=$1; want=$2; shift 2
  T=$(mktemp -d) || exit 2
  git init -q --bare "$T/r.git" && git init -q -b main "$T/w" || exit 2
  ( cd "$T/w" || exit 2
    git config user.name "$NAME"; git config user.email "$MAIL"
    git remote add origin "$T/r.git"
    echo base > base; git add base; git commit -qm "土台"
    # 基準値ファイル（仮のリポなので今の件数）。無いと pre-push が止まるので、"基準値なし" 以外は入れる
    if [ "$label" != 基準値なし ] && [ -n "${REPO_PREFLIGHT_HOME:-}" ]; then
      python3 "$REPO_PREFLIGHT_HOME/scripts/preflight_ratchet.py" --repo . --update-baseline >/dev/null 2>&1
      git add .repo-preflight/ratchet-baseline.json 2>/dev/null && git commit -qm "基準値"
    fi
    git push -q --no-verify origin main >/dev/null 2>&1
    "$@"
    git push -q origin HEAD:main >/dev/null 2>&1 )
  rc=$?
  rm -rf "$T"
  if [ "$rc" -ne 0 ]; then got=止まった; else got=通った; fi
  [ "$got" = "$want" ] || fail=1
  line="$(date '+%Y-%m-%dT%H:%M:%S%z')	$HOST	$label	$got	期待:$want"
  echo "$line"; [ -n "$LOG" ] && echo "$line" >> "$LOG"
}
c_ctrl() { echo a > a; git add a; git commit -qm "対照"; }
c_ident() { echo b > b; git add b; git -c user.name=unlisted -c user.email=unlisted@example.invalid commit -qm "試験"; }
c_sess() { echo c > c; git add c; git commit -qm "試験" -m "Claude-""Session: $SID"; }
c_home() { printf 'p=/%s/zzzz/x\n' home > d; git add d; git commit -qm "試験"; }
c_corp() { echo f > f; git add f; git commit -qm "試験" -m "連絡先 taro@zz-dummy-corp.co.jp"; }
c_none() { echo g > g; git add g; git commit -qm "試験"; }
c_key() { { printf -- '-----BEGIN OPENSSH %s-----\n' "PRIVATE ""KEY"; echo 0000000000000000000000000000000000000000000000000000000000000000; printf -- '-----END OPENSSH %s-----\n' "PRIVATE ""KEY"; } > e; git add e; git commit -qm "試験"; }
one 対照 通った c_ctrl
one 許可にない名義 止まった c_ident
one Session行 止まった c_sess
one ホームパス 止まった c_home
one 鍵 止まった c_key
one 会社メール 止まった c_corp
one 基準値なし 止まった c_none
exit $fail

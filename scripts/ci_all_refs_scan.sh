set +ax
set -euo pipefail
unset GIT_TRACE GIT_TRACE_PACKET GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE2 GIT_TRACE2_EVENT GIT_TRACE2_PERF GIT_TRACE_SETUP
DEFAULT_SHA="$GITHUB_SHA"
target="$(cd "$TARGET" && pwd)"
tool="$(cd "$TOOL" && pwd)"
policy="$tool/references/public-commit-identities.json"
work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/all-refs-scan.XXXXXX")"
# git の stdout・stderr（ref 名・ファイル名・作者を含みうる）はここへだけ書く。
gitlog="$work/git.log"
: > "$gitlog"
quiet() { "$@" >> "$gitlog" 2>&1; }
: > "$work/refs.txt"
: > "$work/tags.txt"
# 各検査の出力（ファイル名・commit・作者などを含みうる）を、種類と件数だけに直してログへ出す。
# 種類にパス・メール・山かっこの形が混ざっていたら、その種類も伏せる。
cat > "$work/summarize.py" <<'PY'
import collections
import json
import re
import sys

mode, path, where = sys.argv[1], sys.argv[2], sys.argv[3]
kinds = collections.Counter()
try:
    with open(path, encoding="utf-8", errors="replace") as stream:
        text = stream.read()
except OSError:
    text = ""
if mode == "issues":
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("失敗:"):
            kinds[line.rsplit(": ", 1)[-1]] += 1
        elif line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("status") == "tool_error":
                kinds[f"tool_error {data.get('reason', '')}".strip()] += 1
elif mode == "ratchet":
    start = text.find("{")
    try:
        data = json.JSONDecoder().raw_decode(text[start:])[0] if start >= 0 else {}
    except ValueError:
        data = {}
    for key in ("increased", "loosened"):
        for item in data.get(key) or []:
            kinds[f"{key} {item.get('check', '')}".strip()] += 1
    if not kinds:
        kinds[f"status {data.get('status', 'tool_error')}"] += 1
elif mode == "gitleaks":
    try:
        findings = json.loads(text) if text.strip() else []
    except ValueError:
        findings = []
    for finding in findings or []:
        kinds[f"gitleaks {finding.get('RuleID', '')}".strip()] += 1
    if not kinds:
        kinds["gitleaks を実行できません"] += 1
if not kinds:
    kinds["種類の読めない失敗"] += 1
for kind, count in sorted(kinds.items()):
    if len(kind) > 80 or re.search(r"[/\\@<>:]|\.\w{1,5}\b", kind):
        kind = "種類を伏せた指摘"
    print(f"赤: {where}: {kind} {count} 件", file=sys.stderr)
PY
# LFS のポインタの形をした blob を、全先端をまとめた履歴（git rev-list --objects）から数える。
# 足してから消したポインタも数える。ポインタの大きさ（1024 バイト）以下の blob だけ中身を読む。
# git の出力は捨てるか、この中で受けるだけ。出すのは件数だけ（名前・ハッシュは出さない）。
cat > "$work/lfs_pointers.py" <<'PY'
import subprocess
import sys

PREFIXES = ("version https://git-lfs.github.com/spec/", "version https://hawser.github.com/spec/")
MAX_POINTER = 1024
# git-lfs は前後の空白を落としてから読む（Go の bytes.TrimSpace）。先頭に空白・タブ・改行があっても
# 中身を展開するので、同じ空白（Go の unicode.IsSpace と同じ集合）を落としてから形を見る。
SPACES = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
repo = sys.argv[1]


def git(args, data=None):
    return subprocess.run(
        ["git", "-C", repo, *args], input=data, capture_output=True, check=True
    ).stdout


tips = sys.stdin.buffer.read()
if not tips.strip():
    raise SystemExit(1)
oids = git(["rev-list", "--objects", "--no-object-names", "--stdin"], tips).split()
if not oids:
    raise SystemExit(1)
count = 0
for start in range(0, len(oids), 1024):
    batch = oids[start:start + 1024]
    listing = git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        b"\n".join(batch) + b"\n",
    ).splitlines()
    small = []
    if len(batch) != len(listing):
        raise SystemExit(1)
    for oid, line in zip(batch, listing):
        name, kind, size = line.split()
        if name != oid or kind not in (b"blob", b"tree", b"commit", b"tag") or not size.isdigit():
            raise SystemExit(1)
        if kind == b"blob" and int(size) <= MAX_POINTER:
            small.append((oid, size))
    if not small:
        continue
    out = git(["cat-file", "--batch"], b"\n".join(oid for oid, _ in small) + b"\n")
    pos = 0
    for oid, size in small:
        header = oid + b" blob " + size + b"\n"
        if not out.startswith(header, pos):
            raise SystemExit(1)
        pos += len(header)
        body = out[pos:pos + int(size)]
        pos += int(size)
        if out[pos:pos + 1] != b"\n":
            raise SystemExit(1)
        pos += 1
        text = body.decode("utf-8", "replace").lstrip(SPACES)
        if text.startswith(PREFIXES) and "\noid sha256:" in text:
            count += 1
    if pos != len(out):
        raise SystemExit(1)
print(count)
PY
# 取得の手順が refs/all-refs/ に入れた枝・タグ・PR の head。タグは指す commit に直す。commit に直せない ref は赤。
# 注釈付きタグは、commit に直す前にタグの object を控えておき、下で作者と注釈文を見る。
while read -r ref; do
  if [ "$(git -C "$target" cat-file -t "$ref" 2>/dev/null)" = tag ]; then
    git -C "$target" rev-parse -q --verify "$ref" 2>/dev/null >> "$work/tags.txt" || true
  fi
  if sha="$(git -C "$target" rev-parse -q --verify "${ref}^{commit}" 2>/dev/null)"; then
    printf '%s\n' "$sha" >> "$work/refs.txt"
  else
    printf 'unresolved\n' >> "$work/refs.txt"
  fi
done < <(git -C "$target" for-each-ref --format='%(refname)' refs/all-refs/ 2>> "$gitlog")
test -s "$work/refs.txt" || { echo "検査する ref がありません" >&2; exit 1; }
quiet git -C "$target" cat-file -e "${DEFAULT_SHA}^{commit}" || { echo "デフォルト枝の commit がありません" >&2; exit 1; }
commits="$(grep -v '^unresolved$' "$work/refs.txt" | sort -u || true)"
total="$(printf '%s\n' "$commits" | grep -c . || true)"
echo "ref $(wc -l < "$work/refs.txt" | tr -d ' ') 件（注釈付きタグ $(wc -l < "$work/tags.txt" | tr -d ' ') 件、検査する commit ${total} 件）"
failed=0
unresolved="$(grep -c '^unresolved$' "$work/refs.txt" || true)"
if [ "$unresolved" -gt 0 ]; then
  echo "赤: commit を指していない ref ${unresolved} 件" >&2
  failed=1
fi
# 共通祖先の blob も一度だけ数える。ref ごとの比較元・checkout は下のループが持つ。
if [ "$total" -gt 0 ]; then
  if lfs_count="$(python "$work/lfs_pointers.py" "$target" <<< "$commits" 2>> "$gitlog")"; then
    if [ "$lfs_count" -gt 0 ]; then
      echo "赤: 全 ref: LFS のポインタの形をした blob（履歴を含む）${lfs_count} 件" >&2
      failed=1
    fi
  else
    echo "赤: 全 ref: LFS のポインタを数えられません" >&2
    failed=1
  fi
fi
# 注釈付きタグの作者（tagger）と注釈文。入れ子のタグもたどる。pre-push と同じ check_pushed_tags を、
# デフォルト枝の版（tool 側）の許可名義で呼ぶ。指摘はファイルにだけ書き、種類と件数に直して出す。
if [ -s "$work/tags.txt" ]; then
  if ! python - "$tool/scripts" "$policy" "$target" "$work/tags.txt" > "$work/tags.out" 2>&1 <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import identity_gate

policy = identity_gate.load_public_policy(sys.argv[2])
failed = 0
with open(sys.argv[4], encoding="utf-8") as stream:
    for tag_sha in stream.read().split():
        try:
            issues = identity_gate.check_pushed_tags(
                policy,
                sys.argv[3],
                f"tag {tag_sha} tag {identity_gate.ZERO_OID}\n",
            )
        except Exception:
            issues = ["タグ: タグを読めない"]
        for issue in issues:
            print("失敗:", issue)
            failed = 1
sys.exit(failed)
PY
  then
    python "$work/summarize.py" issues "$work/tags.out" "注釈付きタグ"
    failed=1
  fi
fi
# ref ごとに別の作業場へ、その ref の履歴とデフォルト枝の commit だけを入れる。
# 他の ref が入ると全履歴の検査（--all）が他の枝の中身を見てしまう（#2 の原因）。
# git の出力は $gitlog へだけ書く。作業場を作れない ref も赤にして、次の ref へ進む。
prepare() {
  local scan="$1" sha="$2"
  quiet git init -q "$scan" &&
    quiet git -C "$target" update-ref refs/all-refs-scan/one "$sha" &&
    quiet git -C "$target" update-ref refs/all-refs-scan/default "$DEFAULT_SHA" &&
    quiet git -C "$scan" fetch -q --no-tags --no-write-fetch-head "$target" refs/all-refs-scan/default refs/all-refs-scan/one &&
    quiet git -C "$target" update-ref -d refs/all-refs-scan/one &&
    quiet git -C "$target" update-ref -d refs/all-refs-scan/default &&
    test -z "$(git -C "$scan" for-each-ref --format='%(refname)' 2>> "$gitlog")" &&
    quiet git -C "$scan" remote add origin "$ORIGIN_URL" &&
    quiet git -C "$scan" config core.symlinks false &&
    mkdir -p "$scan/.git/info" &&
    printf '* -filter\n' > "$scan/.git/info/attributes"
}
# 取り出しは、.gitattributes の filter（LFS を含む。外のコマンドを動かす）を切って行う。
# working-tree-encoding・改行の変換は git 自身が行うので残す（失敗すれば下で赤になる）。
# シンボリックリンクは中身がリンク先の文字列の普通のファイルとして置く（作業場の外を指させない）。
# git が1文字でも出力したら（失敗・警告。ファイル名を含みうる）取り出せなかったものとして扱う。
checkout_quietly() {
  local scan="$1" sha="$2" out="$work/checkout.out"
  GIT_LFS_SKIP_SMUDGE=1 git -C "$scan" -c filter.lfs.process= -c filter.lfs.smudge=cat -c filter.lfs.clean=cat \
    -c filter.lfs.required=false -c advice.detachedHead=false checkout -q --detach "$sha" > "$out" 2>&1 || return 1
  ! test -s "$out"
}
# 同じ commit を指す ref はまとめて1回だけ検査する。ログでは何番目かと ref の件数だけで示す。
index=0
for sha in $commits; do
  index=$((index + 1))
  nrefs="$(grep -cx "$sha" "$work/refs.txt" || true)"
  where="検査 ${index}/${total}（ref ${nrefs} 件）"
  scan="$work/repo-$index"
  echo "== ${where}"
  if ! prepare "$scan" "$sha"; then
    echo "赤: ${where}: 作業場を作れない ref 1 件" >&2
    failed=1
    quiet git -C "$target" update-ref -d refs/all-refs-scan/one || true
    quiet git -C "$target" update-ref -d refs/all-refs-scan/default || true
    rm -rf "$scan" > /dev/null 2>&1 || true
    continue
  fi
  checked_out=1
  if ! checkout_quietly "$scan" "$sha"; then
    # 履歴を読む検査（名義・鍵）は続ける。作業ツリーを読む preflight_ratchet は回さない。
    echo "赤: ${where}: checkout できない ref 1 件" >&2
    failed=1
    checked_out=0
    quiet git -C "$scan" update-ref --no-deref HEAD "$sha" || true
  fi
  # デフォルト枝との分かれ目。整合性の比較元（preflight-gate の preflight-event-base と同じ形）と、
  # identity_gate の範囲（デフォルト枝に無い commit）に使う。分かれ目は HEAD の祖先なので、全履歴の範囲は広がらない。
  ratchet_args=(--repo "$scan" --allowed-identities "$policy" --baseline-base-ref "$DEFAULT_SHA")
  if mb="$(git -C "$scan" merge-base "$DEFAULT_SHA" HEAD 2>/dev/null)"; then
    quiet git -C "$scan" update-ref refs/remotes/origin/all-refs-scan-base "$mb" || true
    ratchet_args+=(--consistency-base-ref refs/remotes/origin/all-refs-scan-base)
    id_range="${mb}..HEAD"
  else
    # デフォルト枝と共通の祖先が無い ref は、全履歴を足した commit として見る
    id_range=HEAD
  fi
  # 名義・本文・足した行の形。例外一覧は tool 側（デフォルト枝の版）だけを読む。
  # デフォルト枝の commit は、その push の CI（preflight-gate）が当時の固定した版で見ている。
  # 例外一覧はファイルの最新の中身の SHA256 だけを持つので、今の一覧で古い中身を見直すと必ず赤になる。
  if ! python "$tool/scripts/identity_gate.py" --public --repo "$scan" --allowed-identities "$policy" --range "$id_range" > "$work/identity.out" 2>&1; then
    python "$work/summarize.py" issues "$work/identity.out" "${where}: identity_gate"
    failed=1
  fi
  # 秘密・個人パス・名義の件数（全履歴）。基準値はその ref の版を使い、デフォルト枝の版より緩めていれば赤。
  if [ "$checked_out" -eq 1 ] && ! python "$tool/scripts/preflight_ratchet.py" "${ratchet_args[@]}" > "$work/ratchet.out" 2>&1; then
    python "$work/summarize.py" ratchet "$work/ratchet.out" "${where}: preflight_ratchet"
    failed=1
  fi
  # 鍵。値は report にだけ伏せ字で書き、ログには規則の種類と件数だけを出す。
  report="$work/gitleaks-$index.json"
  if ! gitleaks git --no-banner --redact --log-level error --config "$tool/.gitleaks.toml" \
      --log-opts=HEAD --report-format json --report-path "$report" "$scan" > /dev/null 2>&1; then
    python "$work/summarize.py" gitleaks "$report" "${where}: gitleaks"
    failed=1
  fi
  rm -rf "$scan" "$report" "$work/identity.out" "$work/ratchet.out" "$work/checkout.out" > /dev/null 2>&1 || true
done
if [ "$failed" -eq 0 ]; then echo "緑: 全 ref"; fi
exit "$failed"

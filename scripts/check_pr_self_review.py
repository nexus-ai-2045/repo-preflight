import hashlib
import pathlib
import re
import sys

path = pathlib.Path("docs/pr-self-review.md")
allowlist_path = pathlib.Path("docs/pr-self-review-trusted-digests.txt")
try:
    text = path.read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    sys.exit(f"{path} unreadable as UTF-8: {exc}")

# (1) 本文の自己整合性
declared = re.search(r"^\| rules_version \| `([0-9a-f]{16})` \|$", text, re.M)
if declared is None:
    sys.exit(f"{path} has no rules_version row")
start = text.find("\n## R1 ")
if start < 0:
    sys.exit(f"{path} has no R1 section")
actual = hashlib.sha256(text[start + 1 :].encode("utf-8")).hexdigest()[:16]
if actual != declared.group(1):
    sys.exit(
        f"{path} was edited by hand: it declares rules_version "
        f"{declared.group(1)} but its body hashes to {actual}. "
        "Regenerate it from the rule exporter (scripts/export_pr_self_review.py)."
    )

# (2) 前置きを含む完全ファイルの同一性
try:
    allow_text = allowlist_path.read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    sys.exit(f"{allowlist_path} unreadable as UTF-8: {exc}")
allowed = {
    line.strip().lower()
    for line in allow_text.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
if not allowed or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in allowed):
    sys.exit(f"{allowlist_path} is empty or malformed")
# checkout は LF、Windows 作業ツリーは CRLF になりうる。内容で比べる
canonical = path.read_bytes().replace(b"\r\n", b"\n")
full = hashlib.sha256(canonical).hexdigest()
if full not in allowed:
    sys.exit(
        f"{path} full-file digest {full} is not in {allowlist_path}. "
        "The preamble is not covered by rules_version, so a distribution "
        "update must refresh the allowlist in the same reviewed change."
    )
print(f"{path} matches rules_version {actual} and allowlisted digest {full}")

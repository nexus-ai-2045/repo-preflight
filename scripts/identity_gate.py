#!/usr/bin/env python3
"""名義・共同作者行・本文の検査（repo-preflight 拡張の下書き）。

個人の型はこのファイルに書かない。非公開の設定から読む:
  1. 環境変数 PREFLIGHT_PRIVATE_PATTERNS_FILE が指すJSON
  2. 環境変数 PREFLIGHT_PRIVATE_PATTERNS（JSON文字列。CIのシークレット用）
設定が無いときは「設定なし」で失敗する（任意にしない）。
"""

from __future__ import annotations
import json, os, re, subprocess, sys
from dataclasses import dataclass, field

TRAILER_RE = re.compile(r"^\s*(co-authored-by|signed-off-by)\s*:\s*(.*)$", re.I | re.M)
# 公開してよい汎用の型（個人名は入れない）
GENERIC_PATH_RES = [
    re.compile(r"[A-Za-z]:[/\\]Us" r"ers[/\\][^/\\\s]+"),
    re.compile(r"/Us" r"ers/[^/\s]+"),
    re.compile(r"/ho" r"me/[^/\s]+"),
    re.compile(r"~[A-Za-z0-9_.-]+/"),  # ホームの省略記法
]


@dataclass
class Policy:
    allow_emails: list[str] = field(default_factory=list)  # 完全一致
    allow_email_res: list[re.Pattern] = field(default_factory=list)
    allow_names: list[str] = field(default_factory=list)
    deny_res: list[re.Pattern] = field(default_factory=list)  # 個人の型（非公開）
    allow_path_res: list[re.Pattern] = field(
        default_factory=list
    )  # 箱の作業場など許す型
    reject_all_coauthors: bool = True


def load_policy() -> Policy:
    raw = None
    p = os.environ.get("PREFLIGHT_PRIVATE_PATTERNS_FILE")
    if p and os.path.exists(p):
        raw = open(p, encoding="utf-8").read()
    elif os.environ.get("PREFLIGHT_PRIVATE_PATTERNS"):
        raw = os.environ["PREFLIGHT_PRIVATE_PATTERNS"]
    if not raw:
        raise SystemExit("設定なし: 非公開の型設定が読めません（失敗扱い）")
    d = json.loads(raw)
    c = lambda xs: [re.compile(x, re.I) for x in xs]
    return Policy(
        allow_emails=[e.lower() for e in d.get("allow_emails", [])],
        allow_email_res=c(d.get("allow_email_patterns", [])),
        allow_names=d.get("allow_names", []),
        deny_res=c(d.get("deny_patterns", [])),
        allow_path_res=c(d.get("allow_path_patterns", [])),
        reject_all_coauthors=d.get("reject_all_coauthors", True),
    )


def email_ok(pol: Policy, email: str) -> bool:
    e = email.strip().lower()
    return e in pol.allow_emails or any(r.fullmatch(e) for r in pol.allow_email_res)


def scan_text(pol: Policy, text: str, where: str) -> list[str]:
    """本文（コミット本文・PR題名と本文・コメント）を検査。見つけた中身は出さず種類だけ返す。"""
    out = []
    for m in TRAILER_RE.finditer(text):
        kind, val = m.group(1).lower(), m.group(2)
        em = re.search(r"<([^>]+)>", val)
        if kind == "co-authored-by" and pol.reject_all_coauthors:
            out.append(f"{where}: 共同作者行あり")
        elif not em or not email_ok(pol, em.group(1)):
            out.append(f"{where}: 許可外の{kind}行")
    for r in pol.deny_res:
        if r.search(text):
            out.append(f"{where}: 個人の型に一致")
            break
    for r in GENERIC_PATH_RES:
        for m in r.finditer(text):
            if not any(a.match(text, m.start()) for a in pol.allow_path_res):
                out.append(f"{where}: 実在パスの型")
                break
    return out


def check_identity(
    pol: Policy, name: str, email: str, role: str, where: str
) -> list[str]:
    out = []
    if not email_ok(pol, email):
        out.append(f"{where}: {role}のメールが許可外")
    if pol.allow_names and name not in pol.allow_names:
        out.append(f"{where}: {role}の名前が許可外")
    if any(r.search(f"{name} {email}") for r in pol.deny_res):
        out.append(f"{where}: {role}が個人の型に一致")
    return out


def git(repo: str, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "-c", "i18n.logOutputEncoding=UTF-8", *a],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
    ).stdout


def check_range(pol: Policy, repo: str, rng: str) -> list[str]:
    """PRの全コミット（基点..HEAD）を1件ずつ検査。マージコミットも含める。"""
    fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"
    out = []
    for rec in git(repo, "log", "--format=" + fmt, rng).split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        h, an, ae, cn, ce, body = rec.split("\x1f", 5)
        w = h[:8]
        out += check_identity(pol, an, ae, "作者", w)
        out += check_identity(pol, cn, ce, "コミッター", w)
        out += scan_text(pol, body, w + " 本文")
    return out


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="名義と本文の検査")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--range", help="例: origin/main..HEAD")
    ap.add_argument("--message-file", help="commit-msg 用")
    ap.add_argument(
        "--pr-event", help="GITHUB_EVENT_PATH のJSON（PR本文・題名・コメント）"
    )
    ns = ap.parse_args(argv)
    pol = load_policy()
    issues: list[str] = []
    if ns.message_file:
        issues += scan_text(
            pol, open(ns.message_file, encoding="utf-8").read(), "コミット文"
        )
    if ns.range:
        issues += check_range(pol, ns.repo, ns.range)
    if ns.pr_event:
        ev = json.load(open(ns.pr_event, encoding="utf-8"))
        pr = ev.get("pull_request") or ev.get("issue") or {}
        issues += scan_text(
            pol, (pr.get("title") or "") + "\n" + (pr.get("body") or ""), "PR本文"
        )
        if ev.get("comment"):
            issues += scan_text(pol, ev["comment"].get("body") or "", "コメント")
    for i in issues:
        print("失敗:", i, file=sys.stderr)
    print(
        json.dumps(
            {"status": "fail" if issues else "pass", "issue_count": len(issues)},
            ensure_ascii=False,
        )
    )
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

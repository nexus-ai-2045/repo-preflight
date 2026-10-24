"""identity_gate の回帰試験。個人の型は試験用の架空の値だけを使う。"""

from synthetic_paths import synthetic_home_path

import hashlib, json, os, subprocess, sys, tempfile, unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import identity_gate as g

POL = {
    "allow_emails": ["1+nexus-bot@users.noreply.github.com"],
    "allow_email_patterns": ["noreply@github\\.com"],
    "allow_names": ["nexus-bot", "GitHub"],
    "deny_patterns": ["kasou-kojin", "kasou\\.alt"],
    "allow_path_patterns": [
        synthetic_home_path("linux", "box/"),
        synthetic_home_path("linux", "runner/"),
    ],
}
OK = ("nexus-bot", "1+nexus-bot@users.noreply.github.com")


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch):
    # 実機の policy / Git 設定を検査fixtureへ持ち込まない。
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.setenv("PREFLIGHT_PRIVATE_PATTERNS", json.dumps(POL))
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["PREFLIGHT_PRIVATE_PATTERNS"] = json.dumps(POL)
        os.environ.pop("PREFLIGHT_PRIVATE_PATTERNS_FILE", None)
        self.p = g.load_policy()

    def tearDown(self):
        os.environ.pop("PREFLIGHT_PRIVATE_PATTERNS", None)


class TextTest(Base):
    def test_共同作者行を拒否(self):
        self.assertTrue(g.scan_text(self.p, "fix\n\nCo-authored-by: x <a@b.c>", "t"))

    def test_小文字の共同作者行も拒否(self):
        self.assertTrue(
            g.scan_text(
                self.p,
                "co-authored-by: nexus-bot <1+nexus-bot@users.noreply.github.com>",
                "t",
            )
        )

    def test_個人の型(self):
        self.assertTrue(g.scan_text(self.p, "see kasou-kojin/repo", "t"))

    def test_実在パス(self):
        self.assertTrue(
            g.scan_text(
                self.p, synthetic_home_path("macos", "someone/x", prefix="path "), "t"
            )
        )

    def test_許可パスは通す(self):
        self.assertEqual(
            g.scan_text(
                self.p, synthetic_home_path("linux", "box/work", prefix="path "), "t"
            ),
            [],
        )

    def test_きれいな本文(self):
        self.assertEqual(g.scan_text(self.p, "直しました", "t"), [])

    def test_出力に中身を出さない(self):
        out = g.scan_text(self.p, "kasou-kojin", "t")
        self.assertFalse(any("kasou" in o for o in out))


class IdentityTest(Base):
    def test_許可名義(self):
        self.assertEqual(g.check_identity(self.p, *OK, "作者", "c"), [])

    def test_許可外メール(self):
        self.assertTrue(
            g.check_identity(self.p, "nexus-bot", "x@" + "gmail.com", "作者", "c")
        )

    def test_許可外の名前(self):
        self.assertTrue(g.check_identity(self.p, "someone", OK[1], "作者", "c"))


class ConfigTest(unittest.TestCase):
    def test_設定なしは失敗(self):
        os.environ.pop("PREFLIGHT_PRIVATE_PATTERNS", None)
        os.environ.pop("PREFLIGHT_PRIVATE_PATTERNS_FILE", None)
        with self.assertRaises(SystemExit):
            g.load_policy()


class RangeTest(Base):
    def _repo(self, commits):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        d = temporary.name
        run = lambda *a, env=None: subprocess.run(
            ["git", "-C", d, *a], check=True, capture_output=True, env=env
        )
        run("init", "-q")
        for (an, ae, cn, ce), msg in commits:
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME=an,
                GIT_AUTHOR_EMAIL=ae,
                GIT_COMMITTER_NAME=cn,
                GIT_COMMITTER_EMAIL=ce,
            )
            run("commit", "-q", "--allow-empty", "-m", msg, env=env)
        return d

    def test_全コミットを見る(self):
        d = self._repo(
            [
                (OK + OK, "a"),
                (OK + ("someone", "x@" + "gmail.com"), "b"),
                (OK + OK, "c"),
            ]
        )
        self.assertTrue(
            any("コミッター" in i for i in g.check_range(self.p, d, "HEAD"))
        )

    def test_本文の共同作者行(self):
        d = self._repo([(OK + OK, "a\n\nCo-authored-by: alt <kasou.alt@example.com>")])
        self.assertTrue(g.check_range(self.p, d, "HEAD"))

    def test_全部きれい(self):
        d = self._repo([(OK + OK, "a"), (OK + OK, "b")])
        self.assertEqual(g.check_range(self.p, d, "HEAD"), [])


class PrEventTest(Base):
    def _ev(self, ev):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(ev, f)
        f.close()
        return g.main(["--pr-event", f.name])

    def test_PR本文に個人の型(self):
        self.assertEqual(
            self._ev({"pull_request": {"title": "t", "body": "kasou-kojin"}}), 1
        )

    def test_コメントに実在パス(self):
        self.assertEqual(
            self._ev(
                {
                    "issue": {"title": "t", "body": ""},
                    "comment": {"body": synthetic_home_path("linux", "someone/a")},
                }
            ),
            1,
        )

    def test_きれいなPR(self):
        self.assertEqual(self._ev({"pull_request": {"title": "t", "body": "直し"}}), 0)


if __name__ == "__main__":
    unittest.main()


def test_standalone_missing_input_does_not_echo_path(tmp_path, capsys):
    result = g.main(
        ["--message-file", str(tmp_path / "synthetic-private-identity-marker")]
    )
    output = capsys.readouterr()
    assert result == 2
    assert json.loads(output.out)["status"] == "tool_error"
    assert "synthetic-private-identity-marker" not in output.out + output.err


def test_standalone_invalid_event_is_safe_error(tmp_path, capsys):
    event = tmp_path / "event.json"
    event.write_text("[]", encoding="utf-8")
    assert g.main(["--pr-event", str(event)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "tool_error"


class DependabotSignoffPolicyTest(unittest.TestCase):
    def test_公式署名だけを許可し別名義メールとCodexは拒否(self):
        allowed = frozenset(
            [
                "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
                "dependabot[bot] <support@github.com>",
            ]
        )
        bot = "dependabot[bot]"
        bot_email = "49699333+dependabot[bot]@users.noreply.github.com"

        def issues_for(signer):
            record = (
                f"tree {'b' * 40}\n"
                f"author {bot} <{bot_email}> 1700000000 +0000\n"
                f"committer {bot} <{bot_email}> 1700000000 +0000\n\n"
                f"Update dependency\n\nSigned-off-by: {signer}"
            )

            def run_git(repo, *args):
                if args == ("rev-list", "--all"):
                    return "a" * 40 + "\n"
                if args == ("cat-file", "commit", "a" * 40):
                    return record
                raise AssertionError("unexpected_git_operation")

            return g.check_public_range(allowed, ".", "--all", git_fn=run_git)

        self.assertEqual(issues_for("dependabot[bot] <support@github.com>"), [])
        for signer in [
            "unapproved-bot <support@github.com>",
            "dependabot[bot] <unapproved@example.invalid>",
            "Codex <support@github.com>",
            "Codex <noreply@openai.com>",
        ]:
            with self.subTest(signer=signer):
                self.assertEqual(len(issues_for(signer)), 1)


@pytest.fixture
def raw_repo(tmp_path):
    def run(*args, data=None):
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(tmp_path), *args],
            input=data,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")

    run("-c", "init.templateDir=", "init", "-q")
    tree = run("hash-object", "-w", "-t", "tree", "--stdin", data=b"").strip()
    valid = f"{OK[0]} <{OK[1]}> 1700000000 +0000"

    def commit(*, author=valid, committer=valid, extra="", body="clean\n", parent=None):
        raw = f"tree {tree}\n"
        if parent:
            raw += f"parent {parent}\n"
        if author is not None:
            raw += f"author {author}\n"
        if committer is not None:
            raw += f"committer {committer}\n"
        raw += extra + "\n" + body
        oid = run(
            "hash-object",
            "-w",
            "--literally",
            "-t",
            "commit",
            "--stdin",
            data=raw.encode("utf-8"),
        ).strip()
        return oid

    return str(tmp_path), commit, run


def both_checks(repo, oid):
    allowed = {f"{OK[0]} <{OK[1]}>"}
    return (
        lambda: g.check_range(g.load_policy(), repo, oid),
        lambda: g.check_public_range(allowed, repo, oid),
    )


@pytest.mark.parametrize("role", ["author", "committer"])
@pytest.mark.parametrize(
    "control", ["\x1f", "\x1e", "\t", "\r", "\x00", "\x7f", "\x85", "\u200b"]
)
@pytest.mark.parametrize("field", ["name", "email"])
def test_raw_commit_control_identity_fails_without_leaking(
    raw_repo, capsys, role, control, field
):
    repo, commit, _ = raw_repo
    if field == "name":
        # 旧formatでは許可メールとcommitterを偽装し、実メールを本文へ追い出せる。
        name = control.join([OK[0], OK[1], OK[0], OK[1]])
        email = "synthetic-unauthorized@example.invalid"
    else:
        name, email = OK[0], OK[1] + control
    oid = commit(**{role: f"{name} <{email}> 1700000000 +0000"})
    for check in both_checks(repo, oid):
        with pytest.raises(ValueError, match="^identity_commit_header_invalid$"):
            check()
    assert g.main(["--repo", repo, "--range", oid]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["reason"] == "identity_input_unavailable"
    assert not output.err
    assert "synthetic-unauthorized" not in output.out
    assert repo not in output.out


@pytest.mark.parametrize(
    "override",
    [
        {"author": None},
        {"committer": None},
        {"extra": f"author {OK[0]} <{OK[1]}> 1700000000 +0000\n"},
        {"extra": f"committer {OK[0]} <{OK[1]}> 1700000000 +0000\n"},
        {"author": f"{OK[0]} <{OK[1]}> invalid +0000"},
        {"committer": f"{OK[0]} <{OK[1]}> 1700000000 +2400"},
        {"committer": f"{OK[0]} <{OK[1]}> 1700000000 +0060"},
        {"committer": f"{OK[0]} <{OK[1]}> 1700000000 UTC"},
        {"committer": f"{OK[0]} <{OK[1]}> 1700000000 +0000\n continuation"},
        {"committer": f"{OK[0]} <{OK[1]}> 1700000000 +0000 trailing"},
    ],
)
def test_raw_commit_malformed_headers_fail_closed(raw_repo, override):
    repo, commit, _ = raw_repo
    oid = commit(**override)
    for check in both_checks(repo, oid):
        with pytest.raises(ValueError, match="^identity_commit_header_invalid$"):
            check()


def test_raw_commit_multiline_signature_and_body_are_preserved(raw_repo):
    repo, commit, _ = raw_repo
    body = "title\n\nordinary\ttext\nrecord\x1eunit\x1f\nfinal\n"
    oid = commit(
        extra="gpgsig -----BEGIN PGP SIGNATURE-----\n \n synthetic-signature\n -----END PGP SIGNATURE-----\n",
        body=body,
    )
    for check in both_checks(repo, oid):
        assert check() == []
    assert list(g.raw_commits(repo, oid, g.git))[0][-1] == body


def test_identity_subset_keeps_full_body_scope(raw_repo):
    repo, commit, _ = raw_repo
    earlier = commit(author="unapproved <unapproved@example.invalid> 1700000000 +0000")
    latest = commit(parent=earlier)
    assert g.check_range(g.load_policy(), repo, latest)
    assert (
        g.check_range(
            g.load_policy(), repo, latest, identity_rng=f"{earlier}..{latest}"
        )
        == []
    )
    earlier_body = commit(
        author="unapproved <unapproved@example.invalid> 1700000000 +0000",
        body="subject\n\nkasou-kojin\n",
    )
    latest_body = commit(parent=earlier_body)
    issues = g.check_range(
        g.load_policy(),
        repo,
        latest_body,
        identity_rng=f"{earlier_body}..{latest_body}",
    )
    assert issues == [f"{earlier_body[:8]} 本文: 個人の型に一致"]


@pytest.mark.parametrize(
    "signer",
    [
        f"REDACTED <{OK[1]}>",
        f"[REDACTED] <{OK[1]}>",
        f"unknown <{OK[1]}>",
        f"{OK[0]}\x1f <{OK[1]}>",
        f"{OK[0]}\x1e <{OK[1]}>",
        f"{OK[0]} <{OK[1]}\t>",
        f"\t{OK[0]} <{OK[1]}>",
        f"{OK[0]} <{OK[1]}>\r",
        f"{OK[0]} <{OK[1]}> trailing",
    ],
)
def test_signed_off_by_name_and_email_are_both_checked(raw_repo, signer):
    repo, commit, _ = raw_repo
    oid = commit(body=f"title\n\nSigned-off-by: {signer}\n")
    for check in both_checks(repo, oid):
        issues = check()
        assert issues
        assert all(signer not in issue for issue in issues)


def test_approved_signoff_and_private_forbidden_email_hash(raw_repo):
    repo, commit, _ = raw_repo
    oid = commit(body=f"title\n\nSigned-off-by: {OK[0]} <{OK[1]}>\n")
    for check in both_checks(repo, oid):
        assert check() == []
    policy = g.load_policy()
    policy.deny_email_sha256s = [hashlib.sha256(OK[1].encode("utf-8")).hexdigest()]
    issues = g.check_range(policy, repo, oid)
    assert any("作者のメールが許可外" in issue for issue in issues)
    assert any("許可外のsigned-off-by行" in issue for issue in issues)


def test_git_ignores_replace_objects(raw_repo):
    repo, commit, run = raw_repo
    invalid = commit(author="unapproved <unapproved@example.invalid> 1700000000 +0000")
    valid = commit()
    run("replace", invalid, valid)
    for check in both_checks(repo, invalid):
        assert check()


@pytest.mark.parametrize(
    "output", ["bad-revision\n", "a" * 40 + "\x1e\n", "a" * 40 + "\n\n"]
)
def test_callback_invalid_commit_id_is_safe_error(output):
    with pytest.raises(ValueError, match="^identity_commit_list_invalid$"):
        g.check_public_range(set(), ".", "HEAD", git_fn=lambda *args: output)


def test_default_git_disables_signature_and_fsmonitor(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"raw\r\n", stderr=b"")

    monkeypatch.setattr(g.subprocess, "run", run)
    assert g.git("synthetic-repo", "cat-file", "commit", "a" * 40) == "raw\r\n"
    command, options = calls[0]
    assert "--no-replace-objects" in command
    assert "core.fsmonitor=false" in command
    assert "log.showSignature=false" in command
    assert not options.get("text")


def test_standalone_reader_does_not_follow_inherited_git_dir(
    raw_repo, tmp_path, monkeypatch
):
    repo, commit, run = raw_repo
    oid = commit(author="Unapproved <not-approved@example.invalid> 1700000000 +0000")
    run("update-ref", "refs/heads/main", oid)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    subprocess.run(
        ["git", "-C", str(foreign), "init", "-q"], check=True, capture_output=True
    )
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    monkeypatch.setenv("PREFLIGHT_PRIVATE_PATTERNS", json.dumps(POL))
    assert g.check_range(g.load_policy(), repo, "--all")
    assert g.check_public_range({f"{OK[0]} <{OK[1]}>"}, repo, "--all")

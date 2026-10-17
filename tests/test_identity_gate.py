"""identity_gate の回帰試験。個人の型は試験用の架空の値だけを使う。"""

import json, os, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import identity_gate as g

POL = {
    "allow_emails": ["1+nexus-bot@users.noreply.github.com"],
    "allow_email_patterns": ["noreply@github\\.com"],
    "allow_names": ["nexus-bot", "GitHub"],
    "deny_patterns": ["kasou-kojin", "kasou\\.alt"],
    "allow_path_patterns": ["/ho" "me/box/", "/ho" "me/runner/"],
}
OK = ("nexus-bot", "1+nexus-bot@users.noreply.github.com")


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
        self.assertTrue(g.scan_text(self.p, "path /Us" "ers/someone/x", "t"))

    def test_許可パスは通す(self):
        self.assertEqual(g.scan_text(self.p, "path /ho" "me/box/work", "t"), [])

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
            g.check_identity(self.p, "nexus-bot", "x@gmail.com", "作者", "c")
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
        d = tempfile.mkdtemp()
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
            [(OK + OK, "a"), (OK + ("someone", "x@gmail.com"), "b"), (OK + OK, "c")]
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
                    "comment": {"body": "/ho" "me/someone/a"},
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

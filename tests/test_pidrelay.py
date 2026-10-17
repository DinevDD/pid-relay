"""Self-tests for the pid-relay toolkit.  Run: python3 -m unittest discover tests"""
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from pidrelay import w3idcheck
from pidrelay.check import check_ownership, validate
from pidrelay.lint import lint_htaccess
from pidrelay.runner import Httpd, probe_namespaces

REPO = Path(__file__).resolve().parent.parent


def make_repo(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="pidrelay-t-"))
    (d / "config").mkdir()
    (d / "config" / "platform.yaml").write_text(textwrap.dedent("""\
        public_url: https://relay.example
        admins: {github: [prof], accounts: [admin]}
        ui_bot: relay-bot
    """))
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content))
    return d


def errors(text, ns="x/g1"):
    return [i.msg for i in lint_htaccess(textwrap.dedent(text), "f", ns) if i.level == "error"]


class LintTests(unittest.TestCase):
    def test_typical_w3id_file_is_clean(self):
        self.assertEqual(errors("""\
            Options +FollowSymLinks
            RewriteEngine on
            RewriteCond %{HTTP_ACCEPT} text/turtle
            RewriteRule ^(.*)$ https://example.org/$1.ttl [R=303,L]
            Header set Vary Accept
            AddType text/turtle .ttl
        """), [])

    def test_forbidden_directive(self):
        self.assertTrue(errors("SetHandler cgi-script"))
        self.assertTrue(errors("RewriteRul ^x$ https://a.b/ [R=302]"))

    def test_proxy_and_cookie_flags(self):
        self.assertTrue(errors("RewriteEngine on\nRewriteRule ^x$ https://a.b/ [P]"))
        self.assertTrue(errors("RewriteEngine on\nRewriteRule ^x$ https://a.b/ [R=302,CO=a:b:c]"))

    def test_relative_target(self):
        self.assertTrue(errors("RewriteEngine on\nRewriteRule ^x$ other/y [R=302,L]"))

    def test_options_execcgi(self):
        self.assertTrue(errors("Options +ExecCGI"))

    def test_header_whitelist(self):
        self.assertTrue(errors("Header set Set-Cookie evil=1"))
        self.assertEqual(errors("Header always append Vary Accept"), [])

    def test_engine_missing(self):
        self.assertTrue(errors("RewriteRule ^x$ https://a.b/ [R=302,L]"))

    def test_redirect_path_must_include_pid(self):
        self.assertTrue(errors("Redirect 302 /x/g1/a https://a.b/"))
        self.assertEqual(errors("Redirect 302 /pid/x/g1/a https://a.b/"), [])

    def test_sections_rejected(self):
        self.assertTrue(errors("<If \"true\">\n</If>"))
        self.assertEqual(errors("<IfModule mod_rewrite.c>\nRewriteEngine on\n</IfModule>"), [])


class TreeTests(unittest.TestCase):
    def test_missing_tests_yaml_and_owners(self):
        d = make_repo({"rules/x/.htaccess": "RewriteEngine on\n"})
        rep = validate(d)
        msgs = " ".join(i.msg for i in rep.errors)
        self.assertIn("tests.yaml", msgs)
        self.assertIn("OWNERS.yaml", msgs)
        shutil.rmtree(d)

    def test_unexpected_file(self):
        d = make_repo({"rules/x/OWNERS.yaml": "accounts: [a]\n", "rules/x/index.php": "<?php"})
        self.assertTrue(any("unexpected file" in i.msg for i in validate(d, run=False).errors))
        shutil.rmtree(d)

    def test_failing_expectation(self):
        d = make_repo({
            "rules/x/OWNERS.yaml": "accounts: [a]\n",
            "rules/x/.htaccess": "RewriteEngine on\nRewriteRule ^a$ https://e.org/a [R=302,L]\n",
            "rules/x/tests.yaml": "tests:\n  - path: a\n    status: 303\n",
        })
        rep = validate(d)
        self.assertEqual(len(rep.failed), 1)
        self.assertFalse(rep.ok)
        shutil.rmtree(d)


class ApacheTests(unittest.TestCase):
    """What Apache itself does with files, independent of the linter."""

    def test_isolation_and_parity(self):
        d = make_repo({
            "rules/x/good/.htaccess": "Options +FollowSymLinks\nRewriteEngine on\n"
                                      "RewriteRule ^(.*)$ https://e.org/$1 [R=302,L]\n",
            "rules/x/bad/.htaccess": "SetHandler cgi-script\n",
            "rules/x/typo/.htaccess": "RewriteEngine on\nRewriteRul ^a$ https://e.org/ [R]\n",
            "rules/x/proxy/.htaccess": "RewriteEngine on\nRewriteRule ^(.*)$ http://127.0.0.1/ [P]\n",
            "rules/x/OWNERS.yaml": "accounts: [a]\n",
            "rules/x/rel/.htaccess": "RewriteEngine on\nRewriteRule ^a$ /pid/x/good/z [R=302,L]\n",
        })
        with Httpd(d / "rules", "https://relay.example") as h:
            st, loc, _ = h.request("/pid/x/good/item?v=1")
            self.assertEqual((st, loc), (302, "https://e.org/item?v=1"))
            broken = dict(probe_namespaces(h))
            self.assertIn("x/bad", broken)
            self.assertIn("x/typo", broken)
            self.assertNotIn("x/good", broken)
            st, _, _ = h.request("/pid/x/proxy/a")
            self.assertNotEqual(st, 200)            # no proxying possible
            st, loc, _ = h.request("/pid/x/rel/a")  # server-relative uses public URL
            self.assertEqual(loc, "https://relay.example/pid/x/good/z")
            st, loc, hd = h.request("/pid/x/good")       # bare directory: like w3id.org
            self.assertEqual((st, loc), (301, "https://relay.example/pid/x/good/"))
            self.assertEqual(hd.get("Access-Control-Allow-Origin"), "*")
            st, _, _ = h.request("/pid/x/OWNERS.yaml")
            self.assertEqual(st, 403)
            st, _, _ = h.request("/etc/passwd")
            self.assertGreaterEqual(st, 400)
        shutil.rmtree(d)


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, text=True).stdout.strip()


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.d = make_repo({
            "rules/course/OWNERS.yaml": "github: [prof2]\naccounts: [teacher]\n",
            "rules/course/g1/OWNERS.yaml": "github: [alice]\naccounts: [group01]\n",
            "rules/course/g2/OWNERS.yaml": "github: [bob]\naccounts: [group02]\n",
        })
        git(self.d, "init", "-q", "-b", "main")
        git(self.d, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(self.d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        self.base = git(self.d, "rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.d)

    def change(self, rel, content="x", trailer=None):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        msg = "change" + (f"\n\nPid-Relay-Account: {trailer}" if trailer else "")
        git(self.d, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(self.d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
        return git(self.d, "rev-parse", "HEAD")

    def test_own_group_ok_other_group_denied(self):
        head = self.change("rules/course/g1/.htaccess")
        self.assertEqual(check_ownership(self.d, self.base, head, "alice"), [])
        self.assertTrue(check_ownership(self.d, self.base, head, "bob"))

    def test_parent_owner_inherits(self):
        head = self.change("rules/course/g2/.htaccess")
        self.assertEqual(check_ownership(self.d, self.base, head, "prof2"), [])

    def test_cannot_add_self_to_owners(self):
        head = self.change("rules/course/g2/OWNERS.yaml", "github: [bob, alice]\n")
        self.assertTrue(check_ownership(self.d, self.base, head, "bob"))
        self.assertEqual(check_ownership(self.d, self.base, head, "prof2"), [])

    def test_new_prefix_and_platform_files_admin_only(self):
        head = self.change("rules/newprefix/OWNERS.yaml", "github: [alice]\n")
        self.assertTrue(check_ownership(self.d, self.base, head, "prof2"))
        self.assertEqual(check_ownership(self.d, self.base, head, "prof"), [])
        head2 = self.change("pidrelay/evil.py")
        self.assertTrue(check_ownership(self.d, head, head2, "alice"))

    def test_ui_bot_uses_trailer(self):
        head = self.change("rules/course/g1/.htaccess", trailer="group01")
        self.assertEqual(check_ownership(self.d, self.base, head, "relay-bot"), [])
        head2 = self.change("rules/course/g1/tests.yaml", trailer="group02")
        self.assertTrue(check_ownership(self.d, head, head2, "relay-bot"))
        head3 = self.change("rules/course/g1/README.md")  # bot commit without trailer
        self.assertTrue(check_ownership(self.d, head2, head3, "relay-bot"))


@unittest.skipUnless(w3idcheck.checker(), "w3id.org checker not configured (PIDRELAY_W3ID_CHECK)")
class W3idCheckerTests(unittest.TestCase):
    def test_findings_are_mapped_to_rules_tree(self):
        d = make_repo({
            "rules/x/OWNERS.yaml": "accounts: [a]\n",
            "rules/x/g/.htaccess": "RewriteEngine on\nRewriteRule ^go/(.*)$ https://$1 [R=302,L]\n",
            "rules/x/g/tests.yaml": "tests:\n  - path: go/e.org\n    status: 302\n",
        })
        rep = validate(d, run=False)
        hits = [i for i in rep.errors if "no-open-redirect" in i.msg]
        self.assertTrue(hits, rep.text())
        self.assertEqual(hits[0].file, "rules/x/g/.htaccess")
        shutil.rmtree(d)


class ExampleTreeTests(unittest.TestCase):
    def test_repository_rules_pass(self):
        rep = validate(REPO)
        self.assertTrue(rep.ok, rep.text())


if __name__ == "__main__":
    unittest.main()

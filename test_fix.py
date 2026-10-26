"""Tests for fix.py. Run: python -m unittest -v

The git tests build commits with plumbing (hash-object, mktree), so they don't
depend on your git config, hooks or signing."""
import os, re, subprocess, tempfile, unittest

import fix

RULES = fix.Rules()
GOOD = f"{RULES.name} <{RULES.email}>"
BAD = "Some Session <session@example.invalid>"
BOT = "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"
WHEN = "1767225600 +0100"  # all fixture commits: 2026-01-01


class RulesTest(unittest.TestCase):
    def test_reads_every_footer_in_check_yml(self):
        with open(fix.CHECK_YML, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"const FOOTERS = \[\n(.*?)\n\s*\];", text, re.S).group(1)
        self.assertEqual(len(RULES.footers), len(block.splitlines()))
        self.assertGreaterEqual(len(RULES.footers), 4)
        self.assertTrue(RULES.name and RULES.email and len(RULES.bots) >= 3)

    def test_identity_matches_the_check(self):
        self.assertTrue(RULES.identity_ok(RULES.name, RULES.email))
        self.assertFalse(RULES.identity_ok(RULES.name, RULES.email.upper()))  # the check compares exactly
        self.assertFalse(RULES.identity_ok("Someone Else", RULES.email))
        self.assertTrue(RULES.identity_ok("GitHub", "noreply@github.com"))
        self.assertTrue(RULES.identity_ok("anything", "41898282+GitHub-Actions[bot]@users.noreply.github.com"))
        self.assertFalse(RULES.identity_ok("Some Session", "session@example.invalid"))

    def test_every_footer_pattern_is_caught_and_removed(self):
        samples = [
            "Co-authored-by: Example <example@example.invalid>",
            "  co-authored-by: Example <example@example.invalid>",
            "Claude-Session: 0123abcd",
            "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)",
            "https://claude.ai/code/session_0123abcd",
        ]
        for line in samples:
            text = f"Fix the widget\n\nBody line.\n\n{line}\n"
            cleaned, found, left = RULES.clean(text)
            self.assertTrue(found, line)
            self.assertEqual(cleaned, "Fix the widget\n\nBody line.\n", line)
            self.assertEqual(left, [], line)

    def test_clean_keeps_everything_else(self):
        text = "Subject\n\nFirst.\n\nCo-authored-by: A <a@example.invalid>\n\nSecond, about co-authored-by: lines.\n"
        cleaned, found, left = RULES.clean(text)
        self.assertEqual(cleaned, "Subject\n\nFirst.\n\nSecond, about co-authored-by: lines.\n")
        self.assertEqual((found, left), (["a Co-authored-by line"], []))
        self.assertEqual(RULES.clean("Nothing to see\n"), ("Nothing to see\n", [], []))

    def test_clean_handles_crlf_bodies(self):
        cleaned, found, _ = RULES.clean("Summary\r\n\r\n- a point\r\n\r\nCo-authored-by: A <a@example.invalid>\r\n")
        self.assertEqual(cleaned, "Summary\r\n\r\n- a point\n")
        self.assertEqual(found, ["a Co-authored-by line"])

    def test_report_never_trips_the_check(self):
        changed = [f"commit abc1234: removed {what} from the message" for _, what in RULES.footers]
        changed += ["commit abc1234: author set to the allowed identity"]
        text = fix.report(RULES, "feature", True, changed, ["the PR title contains a Claude session link: edit it"], False)
        self.assertEqual(RULES.scan(text), [])
        self.assertIn("removed a Co-authored-by line", text)


class GitRepo:
    """A throwaway repository with commits built from plumbing."""

    def __init__(self, path):
        self.git = fix.Git(path)
        self.git("init", "-q", "-b", "main")

    def commit(self, files, parents, message, author=GOOD, committer=GOOD):
        blobs = {name: self.git("hash-object", "-w", "--stdin", input=body.encode()) for name, body in files.items()}
        tree = self.git("mktree", input="".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(blobs.items())).encode())
        raw = f"tree {tree}\n" + "".join(f"parent {p}\n" for p in parents) + \
              f"author {author} {WHEN}\ncommitter {committer} {WHEN}\n\n{message}"
        return self.git("hash-object", "-t", "commit", "-w", "--stdin", input=raw.encode())

    def info(self, sha):
        fmt = "%T%n%P%n%an <%ae>%n%cn <%ce>%n%ad%n%cd%n%B"
        tree, parents, author, committer, adate, cdate, *message = self.git("log", "-1", "--date=raw", f"--format={fmt}", sha).split("\n")
        return dict(tree=tree, parents=parents.split(), author=author, committer=committer, dates=(adate, cdate), message="\n".join(message).strip())


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = GitRepo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        """main: m1 - m2;  PR branch from m1: good - bad author - footer - merge of m2 - bot."""
        c = self.repo.commit
        m1 = c({"a": "1"}, [], "Start\n")
        m2 = c({"a": "1", "m": "main"}, [m1], "Main moves on\n")
        good = c({"a": "2"}, [m1], "Good commit\n")
        bad = c({"a": "3"}, [good], "Session commit\n", author=BAD, committer=BAD)
        footer = c({"a": "4"}, [bad], "Footer commit\n\nCo-authored-by: Example <example@example.invalid>\n")
        merge = c({"a": "4", "m": "main"}, [footer, m2], "Merge main\n")
        bot = c({"a": "4", "m": "main", "d": "bump"}, [merge], "Bump a dependency\n", author=BOT, committer="GitHub <noreply@github.com>")
        self.repo.git("update-ref", "refs/heads/main", m2)
        self.repo.git("update-ref", "refs/heads/pr", bot)
        return dict(m1=m1, m2=m2, good=good, bad=bad, footer=footer, merge=merge, bot=bot)

    def chain(self, head):
        return self.repo.git("rev-list", "--topo-order", head, "--not", "main").split()

    def test_rewrite_fixes_only_what_fails(self):
        c = self.build()
        new, changed, left = fix.rewrite(self.repo.git, RULES, c["bot"], "main")
        self.assertEqual(left, [])
        self.assertEqual(len(changed), 3)  # author, committer, message
        self.assertNotEqual(new, c["bot"])

        old_chain, new_chain = self.chain(c["bot"]), self.chain(new)
        self.assertEqual(len(old_chain), len(new_chain))
        self.assertIn(c["good"], new_chain)  # before the first bad commit: untouched
        for old, now in zip(old_chain, new_chain):
            o, n = self.repo.info(old), self.repo.info(now)
            self.assertEqual(o["tree"], n["tree"])
            self.assertEqual(o["dates"], n["dates"])
            self.assertEqual(RULES.scan(n["message"]), [])
            for role in ("author", "committer"):
                name, email = re.match(r"(.*) <(.*)>", n[role]).groups()
                self.assertTrue(RULES.identity_ok(name, email), n[role])

        merge = self.repo.info(new_chain[1])
        self.assertEqual(merge["message"], "Merge main")
        self.assertEqual(merge["parents"][1], c["m2"])  # main's side of the merge is untouched
        bot = self.repo.info(new)
        self.assertEqual(bot["author"], BOT)  # allowed identities are left as they are
        self.assertEqual(bot["committer"], "GitHub <noreply@github.com>")
        self.assertEqual(self.repo.info(new_chain[2])["message"], "Footer commit")

    def test_clean_branch_is_left_alone(self):
        c = self.repo.commit
        m1 = c({"a": "1"}, [], "Start\n")
        head = c({"a": "2"}, [c({"a": "1", "b": "x"}, [m1], "One\n")], "Two\n")
        self.repo.git("update-ref", "refs/heads/main", m1)
        self.assertEqual(fix.rewrite(self.repo.git, RULES, head, "main"), (head, [], []))

    def test_footer_only_message_is_reported(self):
        m1 = self.repo.commit({"a": "1"}, [], "Start\n")
        head = self.repo.commit({"a": "2"}, [m1], "Co-authored-by: Example <example@example.invalid>\n")
        self.repo.git("update-ref", "refs/heads/main", m1)
        new, changed, left = fix.rewrite(self.repo.git, RULES, head, "main")
        self.assertEqual((new, changed), (head, []))
        self.assertIn("only a footer", left[0])

    def test_signature_is_dropped_from_rewritten_commits(self):
        m1 = self.repo.commit({"a": "1"}, [], "Start\n")
        tree = self.repo.info(m1)["tree"]
        raw = (f"tree {tree}\nparent {m1}\nauthor {BAD} {WHEN}\ncommitter {GOOD} {WHEN}\n"
               "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n\nSigned\n")
        head = self.repo.git("hash-object", "-t", "commit", "-w", "--stdin", input=raw.encode())
        self.repo.git("update-ref", "refs/heads/main", m1)
        self.assertEqual(fix.unparse(*fix.parse(self.repo.git.run("cat-file", "commit", head)[1])), raw.encode())
        new, _, _ = fix.rewrite(self.repo.git, RULES, head, "main")
        self.assertNotIn(b"gpgsig", self.repo.git.run("cat-file", "commit", new)[1])

    def test_push_uses_a_lease(self):
        c = self.build()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as bare:
            subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
            self.repo.git("remote", "add", "origin", bare)
            self.repo.git("push", "-q", "origin", "pr", "main")
            new, _, _ = fix.rewrite(self.repo.git, RULES, c["bot"], "main")

            # Someone pushes meanwhile: the fix must not overwrite it
            self.repo.git("push", "-q", "origin", f"{c['merge']}:refs/heads/pr", "--force")
            self.assertEqual(fix.push(self.repo.git, "pr", c["bot"], new), "moved")
            self.repo.git("push", "-q", "origin", f"{c['bot']}:refs/heads/pr", "--force")

            self.assertIsNone(fix.push(self.repo.git, "pr", c["bot"], new))
            remote = fix.Git(bare)
            self.assertEqual(remote("rev-parse", "refs/heads/pr"), new)


if __name__ == "__main__":
    unittest.main()

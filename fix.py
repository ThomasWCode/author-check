"""Fix what the author check fails on a pull request, where that is safe.

usage: python fix.py --repo [OWNER/]NAME --pr N [--dry-run] [--trigger COMMENT_ID]

Run by .github/workflows/fix.yml, from a "/author-check fix" comment on the
PR (with that repo's own token) or on demand (with FIX_TOKEN); locally it uses
your gh sign-in. The rules are read from check.yml, so it fixes exactly what
the check fails:
 1. commits by a wrong author or committer, or with an AI footer in the
    message: rewritten with the same files, parents and dates, then
    force-pushed with a lease (a push made meanwhile is never overwritten)
 2. footers in the PR description, and in comments and reviews by the repo
    owner: the lines holding them are deleted
Anything else (a footer in the title or in someone else's comment, commits
on a fork) is reported. Posts what it did on the PR; exits 1 if anything is
left to fix by hand.
"""
import argparse, base64, json, os, re, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_YML = os.path.join(HERE, ".github", "workflows", "check.yml")
OWNER = "ThomasWCode"
FLAGS = {"i": re.I, "m": re.M, "s": re.S}
IDENTITY = re.compile(rb"^(.*) <(.*)> (\d+ [+-]\d{4})$")
SIGNATURES = (b"gpgsig", b"gpgsig-sha256")
WORKFLOWS_REFUSED = "without `workflows` permission"


class Rules:
    """The check's allowed identity and footer patterns, read from check.yml."""

    def __init__(self, path=CHECK_YML):
        with open(path, encoding="utf-8") as f:
            text = f.read()

        def find(pattern, what):
            m = re.search(pattern, text, re.S)
            if not m:
                raise RuntimeError(f"check.yml no longer has {what}: update fix.py")
            return m.group(1)

        self.name = find(r"const NAME = '([^']+)';", "NAME")
        self.email = find(r"const EMAIL = '([^']+)';", "EMAIL")
        self.bots = {e.lower() for e in re.findall(r"'([^']+)'", find(r"const BOT_EMAILS = new Set\(\[(.*?)\]\);", "BOT_EMAILS"))}
        block = find(r"const FOOTERS = \[\n(.*?)\n\s*\];", "FOOTERS")
        self.footers = []
        for line in block.splitlines():
            m = re.match(r"^\s*\[/(.*)/([a-z]*), '([^']+)'\],?\s*$", line)
            if not m or set(m.group(2)) - set(FLAGS):
                raise RuntimeError(f"can't read this FOOTERS entry in check.yml: {line.strip()}")
            flags = 0
            for f in m.group(2):
                flags |= FLAGS[f]
            self.footers.append((re.compile(m.group(1).replace("\\/", "/"), flags), m.group(3)))

    def identity_ok(self, name, email):
        """Same test as identityOk in check.yml."""
        return (email or "").lower() in self.bots or (email == self.email and name == self.name)

    def scan(self, text):
        """What the check would find in text, e.g. ["a Co-authored-by line"]."""
        return [what for pattern, what in self.footers if pattern.search(text or "")]

    def clean(self, text):
        """Returns (text without the lines holding a footer, what was found, what is left)."""
        found = self.scan(text)
        if not found:
            return text, [], []
        kept, removed = [], False
        for line in text.splitlines(keepends=True):
            bare = line.rstrip("\r\n")
            if any(pattern.search(bare) for pattern, _ in self.footers):
                removed = True
                continue
            # Don't leave two blank lines where a footer line was
            if removed and not bare.strip() and (not kept or not kept[-1].strip()):
                continue
            removed = False
            kept.append(line)
        out = "".join(kept).rstrip()
        if out and text.endswith("\n"):
            out += "\n"
        return out, found, self.scan(out)


def gh(method, path, body=None, paginate=False):
    """Returns (HTTP status, parsed body); never raises on an HTTP error."""
    args = ["gh", "api", "-X", method, path]
    if paginate:
        args += ["--paginate", "--slurp"]
    if body is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, encoding="utf-8")
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else None
    except json.JSONDecodeError:
        data = None
    if paginate and r.returncode == 0:
        data = [x for page in data for x in page]
    if r.returncode == 0:
        return 200, data
    status = int((data or {}).get("status", 0)) if isinstance(data, dict) else 0
    return status or -1, data if data is not None else r.stderr.strip()


def need(result, what):
    status, data = result
    if status != 200:
        raise RuntimeError(f"{what}: HTTP {status} {str(data)[:200]}")
    return data


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t:
        t = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    if not t:
        raise RuntimeError("no token: set GH_TOKEN or sign in with gh auth login")
    return t


class Git:
    """git in one directory; with a token, its requests to github.com carry it
    in a header (through the environment, so it is in no URL or command line)."""

    def __init__(self, cwd, secret=None):
        self.cwd = cwd
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if secret:
            basic = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
            self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")

    def run(self, *args, input=None, check=True):
        """Returns (exit code, stdout bytes, stderr text)."""
        r = subprocess.run(["git", *args], cwd=self.cwd, env=self.env, input=input, capture_output=True)
        err = r.stderr.decode("utf-8", "replace").strip()
        if check and r.returncode:
            raise RuntimeError(f"git {args[0]} failed: {err[:300]}")
        return r.returncode, r.stdout, err

    def __call__(self, *args, input=None):
        return self.run(*args, input=input)[1].decode("utf-8", "replace").strip()


def parse(raw):
    """Splits a raw commit object into ([[key, value], ...], message)."""
    head, _, message = raw.partition(b"\n\n")
    headers = []
    for line in head.split(b"\n"):
        if line.startswith(b" ") and headers:
            headers[-1][1] += b"\n" + line
        else:
            key, _, value = line.partition(b" ")
            headers.append([key, value])
    return headers, message


def unparse(headers, message):
    return b"\n".join(key + b" " + value for key, value in headers) + b"\n\n" + message


def rewrite(git, rules, head, base):
    """Rewrites the commits on head that are not on base so the check passes.
    Returns (new head, what changed, what is left); new head == head if
    nothing changed. Commits outside the PR, trees and dates are untouched."""
    new, changed, left = {}, [], []
    allowed = f"{rules.name} <{rules.email}>".encode()
    for sha in git("rev-list", "--reverse", "--topo-order", head, "--not", base).split():
        short = sha[:7]
        headers, message = parse(git.run("cat-file", "commit", sha)[1])
        out, dirty = [], False
        encoding = b"utf-8"
        for key, value in headers:
            if key == b"parent":
                parent = value.decode()
                value = new.get(parent, parent).encode()
                dirty |= value.decode() != parent
            elif key in (b"author", b"committer"):
                m = IDENTITY.match(value)
                if not m:
                    left.append(f"commit {short}: can't read its {key.decode()} line")
                elif not rules.identity_ok(m.group(1).decode("utf-8", "replace"), m.group(2).decode("utf-8", "replace")):
                    value = allowed + b" " + m.group(3)
                    changed.append(f"commit {short}: {key.decode()} set to the allowed identity")
                    dirty = True
            elif key == b"encoding":
                encoding = value.strip().lower()
            elif key in SIGNATURES:
                continue  # a rewritten commit's signature no longer matches it
            out.append([key, value])

        try:
            text = message.decode("utf-8") if encoding in (b"utf-8", b"utf8") else None
        except UnicodeDecodeError:
            text = None
        if text is None:
            if rules.scan(message.decode("utf-8", "replace")):
                left.append(f"commit {short}: message isn't UTF-8, so its footer can't be removed")
        else:
            cleaned, found, remaining = rules.clean(text)
            if found and not cleaned.strip():
                left.append(f"commit {short}: message is only a footer: reword it by hand")
            elif found:
                message = cleaned.encode("utf-8")
                changed.append(f"commit {short}: removed {', '.join(found)} from the message")
                dirty = True
                if remaining:
                    left.append(f"commit {short}: message still contains {', '.join(remaining)}")

        new[sha] = git("hash-object", "-t", "commit", "-w", "--stdin", input=unparse(out, message)) if dirty else sha
    return new.get(head, head), changed, left


def push(git, branch, old, new):
    """Force-pushes new to branch only if it is still at old. Returns an error or None."""
    code, out, err = git.run("push", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{old}",
                             "origin", f"{new}:refs/heads/{branch}", check=False)
    if code == 0:
        return None
    err = f"{out.decode('utf-8', 'replace')}\n{err}".strip()  # --porcelain reports rejections on stdout
    if WORKFLOWS_REFUSED in err:
        return "workflows"
    if "stale info" in err or "fetch first" in err or "[rejected]" in err:
        return "moved"
    return err.splitlines()[-1] if err else f"git push exited {code}"


def text_targets(api, pr, number):
    """(where, body, author login, author is a bot, update call) for the PR
    description and every comment and review on the PR."""
    yield "PR description", pr.get("body") or "", pr["user"]["login"], False, lambda b: ("PATCH", f"{api}/pulls/{number}", {"body": b})
    for c in need(gh("GET", f"{api}/issues/{number}/comments?per_page=100", paginate=True), "comments"):
        yield f"comment {c['html_url']}", c.get("body") or "", c["user"]["login"], c["user"]["type"] == "Bot", \
            lambda b, i=c["id"]: ("PATCH", f"{api}/issues/comments/{i}", {"body": b})
    for c in need(gh("GET", f"{api}/pulls/{number}/comments?per_page=100", paginate=True), "review comments"):
        yield f"review comment {c['html_url']}", c.get("body") or "", c["user"]["login"], c["user"]["type"] == "Bot", \
            lambda b, i=c["id"]: ("PATCH", f"{api}/pulls/comments/{i}", {"body": b})
    for r in need(gh("GET", f"{api}/pulls/{number}/reviews?per_page=100", paginate=True), "reviews"):
        yield f"review {r['html_url']}", r.get("body") or "", r["user"]["login"], r["user"]["type"] == "Bot", \
            lambda b, i=r["id"]: ("PUT", f"{api}/pulls/{number}/reviews/{i}", {"body": b})


def report(rules, branch, pushed, changed, left, dry):
    """The PR comment: what was done, how to catch up, what is left."""
    lines = [f"**author-check fix**{' (dry run)' if dry else ''}", ""]
    lines += [f"- {c}" for c in changed] or ["- Nothing needed fixing."]
    if pushed:
        lines += ["", f"Force-pushed `{branch}`. A clone or session on this branch catches up with `git pull --rebase`, which keeps its unpushed commits."]
    if left:
        lines += ["", "Left to fix by hand:"] + [f"- {l}" for l in left]
    text = "\n".join(lines) + "\n"
    # Never post something the check itself would fail
    return text if not rules.scan(text) else "**author-check fix** finished; see the workflow run for details.\n"


def fix(repo, number, dry, trigger=None, rules=None):
    """Returns (comment text, whether anything is left)."""
    rules = rules or Rules()
    api = f"repos/{repo}"
    owner = repo.split("/")[0]
    if trigger and not dry:
        gh("POST", f"{api}/issues/comments/{trigger}/reactions", {"content": "eyes"})
    pr = need(gh("GET", f"{api}/pulls/{number}"), f"PR #{number}")
    if pr["state"] != "open":
        return None, [f"PR #{number} is {'merged' if pr.get('merged') else pr['state']}: nothing to fix"]
    branch, old = pr["head"]["ref"], pr["head"]["sha"]
    changed, left = [], []
    pushed = False

    # 1. commits
    if (pr["head"].get("repo") or {}).get("full_name") != pr["base"]["repo"]["full_name"]:
        left.append("the commits are on a fork, which this can't push to: fix them there (see the README)")
    else:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            git = Git(tmp, token())
            git("init", "-q")
            git("remote", "add", "origin", f"https://github.com/{repo}.git")
            git("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/head",
                f"+refs/heads/{pr['base']['ref']}:refs/remotes/origin/base")
            if git("rev-parse", "refs/remotes/origin/head") != old:
                raise RuntimeError(f"{branch} moved while this was starting: run it again")
            new, c, l = rewrite(git, rules, old, "refs/remotes/origin/base")
            changed += c
            left += l
            if new != old and not dry:
                err = push(git, branch, old, new)
                if err == "workflows":
                    left.append("these commits change workflow files, which the PR's own token may not push: run "
                                f"`gh workflow run fix.yml -R {OWNER}/author-check -f repo={repo.split('/')[1]} -f pr={number}`")
                    changed.clear()
                elif err == "moved":
                    left.append(f"{branch} moved during the fix: run it again")
                    changed.clear()
                elif err:
                    left.append(f"pushing {branch} failed: {err}")
                    changed.clear()
                else:
                    pushed = True

    # 2. text
    if rules.scan(pr["title"]):
        left.append(f"the PR title contains {', '.join(rules.scan(pr['title']))}: edit it")
    for where, body, login, bot, update in text_targets(api, pr, number):
        if bot:
            continue  # the check skips bots
        cleaned, found, remaining = rules.clean(body)
        if not found:
            continue
        if where != "PR description" and login.lower() != owner.lower():
            left.append(f"{where} by {login} contains {', '.join(found)}: ask them to edit it")
            continue
        if not cleaned.strip():
            left.append(f"{where} is only a footer: edit or delete it")
            continue
        if not dry:
            method, path, payload = update(cleaned)
            status, data = gh(method, path, payload)
            if status != 200:
                left.append(f"{where}: editing it failed (HTTP {status}): edit it by hand")
                continue
        changed.append(f"{where}: removed {', '.join(found)}")
        if remaining:
            left.append(f"{where} still contains {', '.join(remaining)}")

    return report(rules, branch, pushed, changed, left, dry), left


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, help=f"[OWNER/]NAME (owner defaults to {OWNER})")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    p.add_argument("--trigger", help="id of the comment that asked for the fix (gets a reaction)")
    a = p.parse_args()
    repo = a.repo if "/" in a.repo else f"{OWNER}/{a.repo}"

    try:
        text, left = fix(repo, a.pr, a.dry_run, a.trigger)
    except RuntimeError as e:
        text, left = f"**author-check fix** failed: {e}\n", [str(e)]
    if text is None:
        text = f"**author-check fix**: {left[0]}\n"
    elif not a.dry_run:
        status, data = gh("POST", f"repos/{repo}/issues/{a.pr}/comments", {"body": text})
        if status != 200:
            print(f"posting the result failed: HTTP {status} {str(data)[:200]}")
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    sys.exit(1 if left else 0)


if __name__ == "__main__":
    main()

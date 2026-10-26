# author-check

A reusable GitHub Actions check for my repositories. On every pull request it checks that:

- every commit's author and committer is `Tom White <tom@thomaswhite.me>` (Dependabot, github-actions and GitHub's own web-flow identity are allowed);
- no commit message, PR title, PR description, or comment or review by a person contains an AI attribution footer (`Co-authored-by:`, `Claude-Session:`, a "Generated with Claude Code" link, or a Claude session link). Comments and reviews by bots are not checked.

It posts the result as the commit status `author-check` on the PR's head commit, so a new comment can fail the check as well as a new commit. A failed check can be fixed with one comment or command (see [Fixing a failed check](#fixing-a-failed-check)). Each repository's "author-check" ruleset requires that status from GitHub Actions; the repository admin can bypass it on pull requests only.

## Use

Copy [`caller/author-check.yml`](caller/author-check.yml) to `.github/workflows/author-check.yml` in a repository, then add the ruleset. The caller's `fix` job needs [`fix.yml`](.github/workflows/fix.yml) on this repo's `main`: GitHub loads every reusable workflow a caller names, even for jobs that are skipped, so without it the whole caller fails to start.

## Fixing a failed check

The fix run ([`fix.py`](fix.py)) repairs what the check fails, using the check's own rules:

- commits by a wrong author or committer, or with a footer in the message, are rewritten with the same files, parents and dates, then force-pushed. Only failing identities change, and a rewritten commit loses its signature;
- footer lines are deleted from the PR description and from comments and reviews by the repo owner.

It reports anything else and leaves it alone: a footer in the PR title or in someone else's comment, or commits on a fork. It posts what it did as a PR comment, and the check runs again by itself.

Start it either way:

- **On the PR:** comment `/author-check fix` (only the repo owner's comments count). It reacts 👀, then answers on the PR. This needs the repo's caller to be the current one from [`caller/`](caller/author-check.yml).
- **On demand:**

  ```sh
  gh workflow run fix.yml -R ThomasWCode/author-check -f repo=NAME -f pr=N
  gh run watch -R ThomasWCode/author-check
  ```

  Add `-f dry_run=true` to see what it would do without changing anything. Locally, `python fix.py --repo NAME --pr N [--dry-run]` does the same with your `gh` sign-in (Python 3, git and the GitHub CLI, no packages).

A comment uses the repo's own Actions token, which may not push commits that change workflow files; the run then says so and gives the on-demand command, which uses `FIX_TOKEN`.

After a force-push, a clone or session on that branch catches up with `git pull --rebase`, which drops the old copies of the rewritten commits and keeps any unpushed ones. Run the fix before a review round: review threads on the old commits become outdated, and replies naming a commit would name one that no longer exists.

By hand:

- Wrong author (e.g. a cloud session's commits): on the PR branch, run `git rebase -r --exec "git commit --amend --no-edit --reset-author" origin/main` and force-push.
- A footer in a commit message: `git rebase -i` and reword the commit, then force-push.
- A footer in the PR description or a comment: edit it out; the edit re-runs the check.

### The token

The on-demand run needs a fine-grained personal access token in this repository's secret `FIX_TOKEN`:

- **Repository access:** All repositories.
- **Permissions:** Contents, Workflows, Pull requests and Issues (all read and write). Metadata (read) is added automatically.
- **Expiry:** a year. Once it has expired, the on-demand run fails until you make a new token and replace the secret; the comment trigger doesn't use it.

## Tests

```sh
python -m unittest -v
```

They also check that `fix.py` can still read the rules in `check.yml`, so change both together. CI runs them on every PR.

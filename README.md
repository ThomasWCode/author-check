# author-check

A reusable GitHub Actions check for my repositories. On every pull request it checks that:

- every commit's author and committer is `Tom White <tom@thomaswhite.me>` (Dependabot, github-actions and GitHub's own web-flow identity are allowed);
- no commit message, PR title, PR description, or comment or review by a person contains an AI attribution footer (`Co-authored-by:`, `Claude-Session:`, a "Generated with Claude Code" link, or a Claude session link). Comments and reviews by bots are not checked.

It posts the result as the commit status `author-check` on the PR's head commit, so a new comment can fail the check as well as a new commit. Each repository's "author-check" ruleset requires that status from GitHub Actions; the repository admin can bypass it on pull requests only.

## Use

Copy [`caller/author-check.yml`](caller/author-check.yml) to `.github/workflows/author-check.yml` in a repository, then add the ruleset.

## Fixing a failed check

- Wrong author (e.g. a cloud session's commits): on the PR branch, run `git rebase -r --exec "git commit --amend --no-edit --reset-author" origin/main` and force-push.
- A footer in a commit message: `git rebase -i` and reword the commit, then force-push.
- A footer in the PR description or a comment: edit it out; the edit re-runs the check.

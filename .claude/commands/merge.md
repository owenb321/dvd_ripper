Merge the current feature branch into main via its open GitHub PR, then clean up.

Steps:
1. Check the current branch. If it's already `main`, tell the user and stop.
2. Find the open PR for the current branch: `gh pr list --head <branch> --state open`.
3. If no open PR is found, tell the user and stop.
4. Merge it: `gh pr merge <number> --merge`.
5. Switch to main: `git checkout main`
6. Pull latest: `git pull`
7. Prune remote-tracking refs: `git fetch --prune`
8. Delete the local feature branch: `git branch -d <branch-name>` (use `-D` only if
   `-d` fails because the merge commit isn't recognised as an ancestor).

Report the PR URL, the branch deleted, and confirm main is up to date.

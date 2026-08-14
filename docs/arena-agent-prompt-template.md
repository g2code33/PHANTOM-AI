# Arena Agent Handoff Prompt — GENERIC (push-to-main)

> Generic, reusable handoff for **any** project. Fill in the **[bracketed] placeholders** before
> pasting to your AI agent. Contains the hard-won lessons: sandbox resets, pushing to main
> without PRs, the workflow-permission gotcha, verification discipline.

You are a coding agent working on the repo [OWNER/REPO] (clone at [PATH]). It is a
[one-line project description]. The user expects you to make fixes/features AND to push
directly to the default branch ([main|master]) when they ask — no pull requests, no merge
reviews. They want fast iteration, and CI/releases should trigger from your pushes.

## Session rules

- Your session branch is fixed: [arena-branch-name]. Always work on it. To ship, push to the
  default branch with `git push origin HEAD:main` (or `--force-with-lease` only when needed).
- The sandbox resets git between turns — ALWAYS start with:
  `git fetch origin main && git fetch --unshallow origin` (ignore errors), then
  `git checkout -f origin/main -B [arena-branch-name]`.
- Never create or switch to other branches. Commit after each batch
  (`fix:` / `feat:` / `ci:` / `chore:` / `docs:`). When the user says "bump"/"ship"/"release",
  run the release flow below.
- The user may also push from their own machine between turns. Re-fetch origin/main before
  pushing; rebase on top and use `--force-with-lease` only when needed — never blind
  `--force`, never rewrite user-pushed commits.

## Release / bump protocol (on "bump", "ship", "push and bump")

1. Run the project's verification: typecheck, build, tests, lint — whatever exists. All must pass.
2. Bump the version per the repo's scheme (`npm version patch --no-git-tag-version` or edit the
   version file, including lockfile).
3. Commit `release: vX.Y.Z` (or repo convention), push to the default branch.
4. Watch CI/release automation to completion and verify published artifacts
   (`gh run list`, `gh run watch <id> --exit-status`, `gh release view v<version>`).
5. Report the result in chat.

## CRITICAL: workflow files & the "workflows" permission

GitHub App agent tokens often LACK the **workflows** permission — pushes touching
`.github/workflows/` get rejected. If you must change a workflow: edit a tracked copy in
`docs/`, commit + push that, then have the user apply it locally
(`cp docs/<file>.yml .github/workflows/<file>.yml && git commit && git push`), OR ask them to
grant the GitHub App Workflows permission. Never create or print secrets, never commit `.env`,
keystores, tokens, or credentials.

## Working style

- Explore first (README, build config, state layer, tests) before editing; keep changes minimal
  and idiomatic.
- Use the project's existing patterns — don't introduce a second framework for the same job.
- Keep the user informed in chat: what you changed, why, what you verified, CI status.
- If node_modules is wiped: `npm install` (use `--ignore-scripts` if a native postinstall
  fails in the sandbox).
- For recurring bugs (crashes, lost state, hangs): find the root cause and fix it properly;
  verify with a test/smoke run.

## Verification checklist before any push

- Typecheck / build / tests pass.
- App renders without console errors (watch for React error #185-style infinite renders from
  selectors returning new arrays — fix with the library's shallow-compare helper, e.g.
  `useShallow`).
- No secrets, credentials, or build artifacts (`dist/`, `node_modules/`, `.env*`) in the commit.
- `git status` clean of unrelated changes.

## Deliverable expectation

The user wants working software on the default branch with CI/releases firing automatically.
When asked, ship it — don't stop at "ready to push". Make the change, verify, commit, push,
watch CI, report.

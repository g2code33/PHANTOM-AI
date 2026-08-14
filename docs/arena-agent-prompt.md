# Arena Agent Handoff Prompt — CLINICAL Rx (push-to-main)

> Repo: **g2code33/CLINICAL-RX-** · Clone: **[PATH]** · Project: **[one-line description of the
> clinical desktop + Android app]**
>
> Desktop (Electron) + Android (Capacitor) application with the release-on-push build structure
> below. This handoff is the CLINICAL Rx–specific version of
> `docs/arena-agent-prompt-template.md` — the generic one — with the repo's concrete build
> facts filled in (derived from the repo's `docs/workflow-build-desktop.yml` /
> `.github/workflows/build-desktop.yml`).

You are a coding agent working on this repo (clone at [PATH]). The user expects you to make
fixes/features AND to push directly to `main` when they ask — no pull requests, no merge
reviews. Pushing to `main` triggers the desktop + Android build and publishes a GitHub Release.

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

## Build & release structure (reference)

- **Workflow**: `.github/workflows/build-desktop.yml` — editable copy kept at
  `docs/workflow-build-desktop.yml`. Triggers: `push: main` + `pull_request` +
  `workflow_dispatch`; `permissions: contents: write`. Two jobs:
  - `build` (non-main, matrix ubuntu + windows): compile + upload artifacts only; ubuntu leg
    also `npx cap sync android` + `assembleDebug` APK artifact.
  - `release` (main, matrix ubuntu + windows): `npm run dist -- --publish always` with
    `GH_TOKEN` + `EP_GH_IGNORE_TIME: 'true'` → auto-creates the GitHub Release; ubuntu leg
    builds `assembleRelease` APK and uploads `clinical-rx-<version>.apk` to the same release.
- **Electron**: `electron-builder` config in `package.json` — `appId`, `productName`
  (ClinicalRx), `files`, `asarUnpack: ["dist/**/*","build/**/*"]` (blank-window fix).
  Targets: Windows `nsis` (`ClinicalRx-Setup-<ver>.exe` + `latest.yml`), Linux
  `deb` (`clinical-rx_<ver>_amd64.deb`) + `AppImage` (`ClinicalRx-<ver>.AppImage` +
  `latest-linux.yml`). Main process: `electron/main.ts` (compiled by `tsc` to `dist-electron`).
- **Android**: Capacitor 8 (`@capacitor/core`/`cli`/`android`), `capacitor.config.ts`
  (`webDir: dist`), committed `android/` project, icons/splash from `resources/icon.png`.
  Requires **Node ≥ 22** and **JDK 21**.
- **Signing**: Android secrets `ANDROID_KEYSTORE_BASE64` / `ANDROID_KEYSTORE_PASSWORD` /
  `ANDROID_KEYSTORE_ALIAS` / `ANDROID_KEYSTORE_KEY_PASSWORD`; `build.gradle` decodes the
  keystore at build time and signs release with PKCS12 fallback to the debug key so the build
  never breaks. Never print/commit secrets; keystores gitignored.

## Release / bump protocol (on "bump", "ship", "push and bump")

1. Verify: `npm run typecheck`, web build, smoke; if Android touched:
   `npx cap sync android` + `./gradlew assembleDebug` (in `android/`).
2. Bump the version — `npm version patch --no-git-tag-version` (updates `package.json` +
   `package-lock.json`); `version` in `package.json` is the ONLY trigger for a new release and
   in-app update. (`npm run release:patch` does bump + commit + push in one step.)
3. Commit `release: vX.Y.Z`, push to `main`.
4. Watch CI to completion: `gh run watch <id> --exit-status`; then verify
   `gh release view v<version> --json assets` lists **all** of:
   `ClinicalRx-Setup-<ver>.exe`, `clinical-rx_<ver>_amd64.deb`, `ClinicalRx-<ver>.AppImage`,
   `clinical-rx-<ver>.apk`, `latest.yml`, `latest-linux.yml`.
5. Report the result in chat.

## CRITICAL: workflow files & the "workflows" permission

GitHub App agent tokens often LACK the **workflows** permission — pushes touching
`.github/workflows/` get rejected. If you must change a workflow: edit the tracked copy in
`docs/` (`docs/workflow-build-desktop.yml`), commit + push that, then have the user apply it
locally (`cp docs/workflow-build-desktop.yml .github/workflows/build-desktop.yml && git commit && git push`),
OR ask them to grant the GitHub App Workflows permission. Never create or print secrets, never
commit `.env`, keystores, tokens, or credentials.

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

The user wants working software on `main` with CI/releases firing automatically. When asked,
ship it — don't stop at "ready to push". Make the change, verify, commit, push, watch CI,
report.

# Arena Agent Prompt — Auto-Build Windows exe + Ubuntu deb + Android APK on GitHub, Release on Push to main

> Applied to **g2code33/PHANTOM-AI** (clone at `/home/user/PHANTOM-AI`). Project description:
> **PHANTOM + CODED** — a personal AI computer operator: two independent AI entities
> (Phantom, general-purpose; Coded, technical) on one shared Python/FastAPI agent framework
> with a static web UI in `ui/`, per-agent NVIDIA keys, tools, memory, permissions, kill switch.

This document records the build-and-release structure (Electron desktop wrapper + Capacitor
Android + GitHub Actions release-on-push), so that:

- Pushing to `main` immediately triggers a build of: **1)** Windows installer (`.exe`),
  **2)** Ubuntu/Linux package (`.deb` + `.AppImage`), **3)** Android app (`.apk`)
- All artifacts publish to a GitHub Release automatically (no manual steps, no tags required)
- `latest.yml` / `latest-linux.yml` are included so desktop installs self-update

## Build

1. **Electron** — `electron-builder`, Windows `nsis` (`.exe` + `latest.yml`), Linux
   `deb` + `AppImage` (+ `latest-linux.yml`). Set `appId`, `productName`, `files`,
   `asarUnpack: ["dist/**/*","build/**/*"]` (fixes blank-window bug).
   - `electron/main.ts` is compiled by `tsc` to `dist-electron/main.js`; the main process
     spawns the Python backend (`python -m phantom_ai.main --port-file …`), waits for the
     port file, then loads `http://127.0.0.1:<port>` in a locked-down `BrowserWindow`.
2. **Android** — Capacitor 8 (`@capacitor/core` / `@capacitor/cli` / `@capacitor/android`),
   `capacitor.config.ts` with `webDir: "dist"`, committed `android/` project, icons/splash
   generated from `resources/icon.png` via `@capacitor/assets`. Requires **Node ≥ 22** and
   **JDK 21** in CI.
3. **Workflow** — `.github/workflows/build-desktop.yml` (editable copy kept at
   `docs/workflow-build-desktop.yml`): on `push: main` + `pull_request` +
   `workflow_dispatch`, `permissions: contents: write`. Two jobs:
   - `build` (non-main, matrix `ubuntu` + `windows`): compile + upload artifacts only;
     the ubuntu leg also `cap sync android` + `assembleDebug` APK artifact.
   - `release` (main, matrix `ubuntu` + `windows`): `npm run dist -- --publish always`
     with `GH_TOKEN` + `EP_GH_IGNORE_TIME: 'true'` → auto-creates the GitHub Release;
     the ubuntu leg builds `assembleRelease` APK and uploads
     `phantom-coded-<version>.apk` to the same release.
4. **Signing** — Android secrets `ANDROID_KEYSTORE_BASE64` / `ANDROID_KEYSTORE_PASSWORD` /
   `ANDROID_KEYSTORE_ALIAS` / `ANDROID_KEYSTORE_KEY_PASSWORD`; `build.gradle` injects the
   keystore at build time and falls back to the debug key (PKCS12) so the build never
   breaks. Secrets are never printed/committed; keystores are gitignored.
5. **Versioning** — the `version` in `package.json` is the only trigger for a new release +
   in-app update; `release:patch` npm script:
   `npm version patch --no-git-tag-version && git add -A && git commit -m "release: v$(...)" && git push origin main`.

## Critical constraints

- The bot token usually lacks the **workflows** permission — pushing `.github/workflows/`
  gets rejected. Keep the editable workflow at `docs/workflow-build-desktop.yml`, push that,
  and have the user apply it locally
  (`cp docs/workflow-build-desktop.yml .github/workflows/build-desktop.yml && git commit && git push`),
  or get Workflows permission granted.
- Sandbox resets: start with `git fetch origin main && git fetch --unshallow origin`, then
  `git checkout -f origin/main -B <arena-branch>`. Push to main with
  `git push origin HEAD:main`; no PRs unless asked.

## Verify before shipping

- [ ] `npm run typecheck` (tsc --noEmit)
- [ ] `npm run web:build` (copies `ui/` → `dist/`)
- [ ] smoke: `.venv/bin/python -m pytest tests/ -q` (Python backend unaffected)
- [ ] `npx cap sync android` + (on CI) `./gradlew assembleDebug` pass
- [ ] electron-builder `dist` passes on CI (Linux + Windows)
- [ ] after push: `gh run watch` + `gh release view v<version> --json assets` shows all
      6+ assets
- [ ] no secrets/artifacts in git

## Success criteria

A push to `main` produces `PhantomCoded-Setup-<ver>.exe`, `phantom-coded_<ver>_amd64.deb`,
`PhantomCoded-<ver>.AppImage`, `phantom-coded-<ver>.apk`, `latest.yml`, `latest-linux.yml`
on a GitHub Release, with desktop self-update working.

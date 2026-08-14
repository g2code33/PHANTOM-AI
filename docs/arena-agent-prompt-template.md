# Arena Agent Prompt — generic push-to-main template

> Fill the placeholders and paste to your AI agent.
> Repo: **[OWNER/REPO]** · Clone: **[PATH]** · Project: **[project description]**

## Goal

Set up the same build-and-release structure used on `g2code33/CLINICAL-RX-` so that:

- Pushing to `main` immediately triggers builds of: **1)** Windows installer (`.exe`),
  **2)** Ubuntu/Linux package (`.deb` + `.AppImage`), **3)** Android app (`.apk`)
- All artifacts publish to a GitHub Release automatically (no manual steps, no tags required)
- `latest.yml` / `latest-linux.yml` are included so desktop installs self-update

## Reference to study

`g2code33/CLINICAL-RX-` →
`.github/workflows/build-desktop.yml`, `docs/workflow-build-desktop.yml`,
`package.json` (electron-builder config), `electron/main.ts`,
`android/` + `capacitor.config.ts`.

## Build

1. **Electron** — electron-builder, Windows `nsis` (`.exe` + `latest.yml`), Linux
   `deb` + `AppImage` (+ `latest-linux.yml`). Set `appId`, `productName`, `files`,
   `asarUnpack: ["dist/**/*","build/**/*"]` (fixes blank-window bug).
2. **Android** — Capacitor 8 (`@capacitor/core`/`cli`/`android`), `capacitor.config.ts`
   (`webDir: dist`), commit the `android/` project, icons/splash from `resources/icon.png`.
   Requires **Node ≥ 22** and **JDK 21**.
3. **Workflow** — `.github/workflows/build-desktop.yml`: on `push: main` + `pull_request` +
   `workflow_dispatch`, `permissions: contents: write`. Two jobs:
   - `build` (non-main, matrix ubuntu+windows): compile + upload artifacts only; ubuntu leg
     also `cap sync android` + `assembleDebug` APK artifact.
   - `release` (main, matrix ubuntu+windows): `npm run dist -- --publish always` with
     `GH_TOKEN` + `EP_GH_IGNORE_TIME: 'true'` → auto-creates the GitHub Release; ubuntu leg
     builds `assembleRelease` APK and uploads `<app>-<version>.apk` to the same release.
4. **Signing** — Android secrets `ANDROID_KEYSTORE_BASE64` / `ANDROID_KEYSTORE_PASSWORD` /
   `ANDROID_KEYSTORE_ALIAS` / `ANDROID_KEYSTORE_KEY_PASSWORD`; inject keystore at build time;
   `build.gradle` signs release with PKCS12 fallback to the debug key so the build never
   breaks. Never print/commit secrets; gitignore keystores.
5. **Versioning** — `version` in `package.json` is the only trigger for a new release +
   in-app update; add `release:patch` npm script
   (`npm version patch --no-git-tag-version && git add -A && git commit -m "release: v$(…)" && git push origin main`).

## Critical

- A bot token usually lacks the **workflows** permission — pushing `.github/workflows/`
  gets rejected. Keep the editable workflow at `docs/workflow-build-desktop.yml`, push that,
  and have the user apply it locally
  (`cp docs/workflow-build-desktop.yml .github/workflows/build-desktop.yml && git commit && git push`),
  or get Workflows permission granted.
- Sandbox resets: `git fetch origin main && git fetch --unshallow origin`, then
  `git checkout -f origin/main -B <arena-branch>`. Push to main via
  `git push origin HEAD:main`; no PRs unless asked.

## Verify before shipping

Typecheck → web build → smoke → electron build → `cap sync` + `assembleDebug` all pass;
after push `gh run watch` + `gh release view v<version> --json assets` shows all 6+ assets;
no secrets/artifacts in git.

## Success criteria

A push to `main` produces `<Product>-Setup-<ver>.exe`, `<app>_<ver>_amd64.deb`,
`<Product>-<ver>.AppImage`, `<app>-<ver>.apk`, `latest.yml`, `latest-linux.yml` on a GitHub
Release, desktop self-update working.

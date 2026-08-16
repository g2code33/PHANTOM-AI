/**
 * PHANTOM + CODED — Electron main process.
 *
 * The desktop shell is a thin wrapper around the Python backend:
 *   1. spawn the backend (`python -m phantom_ai.main --port-file …`)
 *   2. wait for the port file, then load http://127.0.0.1:<port>
 *   3. shut the backend down when the window closes.
 *
 * When packaged, the Python sources live under app.asar.unpacked
 * (see asarUnpack in package.json). The Python interpreter is looked up in
 * PHAI_PYTHON, then .venv/bin/python, then python3/python on PATH. Bundling a
 * full standalone Python (e.g. PyInstaller) into the installer is a documented
 * follow-up; until then the packaged app needs a Python 3.10+ on the machine.
 */

import { app, BrowserWindow, dialog, ipcMain, Menu, MenuItemConstructorOptions, nativeImage, shell, Tray } from "electron";
import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

// Self-update support (electron-updater, GitHub Releases). Only active in the
// packaged app; harmless no-op elsewhere.
import { autoUpdater } from "electron-updater";

let backend: ChildProcess | null = null;
let backendPort = 0;
let mainWindow: BrowserWindow | null = null;
let updateStatus: Record<string, unknown> = { state: "idle" };
let tray: Tray | null = null;
let isQuitting = false;

// --------------------------------------------------------------------------
// Linux sandbox fallback
//
// Electron's Chromium sandbox needs /opt/<App>/chrome-sandbox to be owned by
// root with mode 4755 (setuid). The deb's after-install hook
// (build/after_install.sh) sets that up, but if it ever isn't (AppImage,
// custom installs, filesystems without setuid), Chromium aborts with
// "The SUID sandbox helper binary was found, but is not configured correctly".
// Detect that and fall back to --no-sandbox so the app still opens.
// --------------------------------------------------------------------------

function ensureLinuxSandbox() {
  if (process.platform !== "linux") return;
  if (!app.isPackaged) return; // dev runs are fine
  const candidates = [
    path.join(path.dirname(process.execPath), "chrome-sandbox"),
    path.join(process.resourcesPath || "", "chrome-sandbox"),
    "/opt/Phantom/chrome-sandbox",
    "/opt/PhantomCoded/chrome-sandbox",
  ];
  for (const candidate of candidates) {
    try {
      const st = fs.statSync(candidate);
      if ((st.mode & 0o4000) !== 0) return; // setuid bit present → sandbox OK
    } catch {
      /* keep looking */
    }
  }
  console.warn(
    "[main] chrome-sandbox is not setuid root — Chromium sandbox unavailable; " +
    "falling back to --no-sandbox. Fix permanently with: " +
    "sudo chown root:root /opt/Phantom/chrome-sandbox && sudo chmod 4755 /opt/Phantom/chrome-sandbox",
  );
  app.commandLine.appendSwitch("no-sandbox");
}

ensureLinuxSandbox();

function sendUpdateStatus(status: Record<string, unknown>) {
  updateStatus = status;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("update:status", status);
  }
}

// --------------------------------------------------------------------------
// self-update wiring
// --------------------------------------------------------------------------

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function initUpdater() {
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on("checking-for-update", () =>
    sendUpdateStatus({ state: "checking" }));
  autoUpdater.on("update-available", (info) =>
    sendUpdateStatus({ state: "available", version: info.version }));
  autoUpdater.on("update-not-available", (info) =>
    sendUpdateStatus({ state: "up-to-date", version: app.getVersion() }));
  autoUpdater.on("download-progress", (p) =>
    sendUpdateStatus({ state: "downloading", percent: Math.round(p.percent) }));
  autoUpdater.on("update-downloaded", (info) =>
    sendUpdateStatus({ state: "ready", version: info.version }));
  autoUpdater.on("error", (err) =>
    sendUpdateStatus({ state: "error", message: errMsg(err) }));

  ipcMain.handle("update:check", async () => {
    if (!app.isPackaged) {
      return { state: "dev", message: "updates are only available in the packaged app" };
    }
    try {
      await autoUpdater.checkForUpdates();
      // the check triggers update-not-available/update-available/error events
      // which update `updateStatus` BEFORE the promise resolves — return that
      // live status so the renderer never clobbers it with a transient
      // "checking" (which stuck the button on 'checking for updates…').
      return { ...updateStatus, result: true };
    } catch (err) {
      return { state: "error", message: errMsg(err) };
    }
  });

  ipcMain.handle("update:download", async () => {
    if (!app.isPackaged) return { state: "dev" };
    try {
      await autoUpdater.downloadUpdate();
      return { state: "downloading" };
    } catch (err) {
      return { state: "error", message: String(err) };
    }
  });

  ipcMain.handle("update:install", async () => {
    if (!app.isPackaged) return { state: "dev" };
    autoUpdater.quitAndInstall();
    return { state: "installing" };
  });

  ipcMain.handle("update:version", () => app.getVersion());
}

// --------------------------------------------------------------------------
// backend discovery & lifecycle
// --------------------------------------------------------------------------

function resourceRoot(): string {
  const unpacked = path.join(process.resourcesPath || "", "app.asar.unpacked");
  return fs.existsSync(unpacked) ? unpacked : path.join(__dirname, "..");
}

function commandExists(cmd: string): boolean {
  const { spawnSync } = require("child_process") as typeof import("child_process");
  const res = spawnSync("which", [cmd], { stdio: "ignore" });
  return res.status === 0;
}

function findPython(): string | null {
  const envPy = process.env.PHAI_PYTHON;
  if (envPy && fs.existsSync(envPy)) return envPy;
  const venv = path.join(resourceRoot(), ".venv", "bin", "python");
  if (fs.existsSync(venv)) return venv;
  for (const cand of ["python3", "python"]) {
    if (commandExists(cand)) return cand;
  }
  return null;
}

interface BackendCommand {
  command: string;
  args: string[];
}

/**
 * Locate the backend to spawn, in priority order:
 *   1. PHAI_BACKEND env override
 *   2. the bundled PyInstaller backend (resources/backend/phantom-backend[.exe])
 *   3. a system Python running `python -m phantom_ai.main`
 */
function findBackendCommand(): BackendCommand | null {
  const envBackend = process.env.PHAI_BACKEND;
  if (envBackend && fs.existsSync(envBackend)) {
    return { command: envBackend, args: [] };
  }
  if (app.isPackaged) {
    const exeName = process.platform === "win32" ? "phantom-backend.exe" : "phantom-backend";
    const resources = process.resourcesPath || "";
    // onedir layout: resources/backend/phantom-backend/<exe>
    const bundledDir = path.join(resources, "backend", "phantom-backend");
    const candidate = fs.existsSync(bundledDir) && fs.statSync(bundledDir).isDirectory()
      ? path.join(bundledDir, exeName)
      : path.join(resources, "backend", exeName);
    if (fs.existsSync(candidate)) {
      return { command: candidate, args: [] };
    }
  }
  const python = findPython();
  if (python) {
    return { command: python, args: ["-m", "phantom_ai.main"] };
  }
  return null;
}

function waitForPortFile(portFile: string, timeoutMs = 60_000): Promise<number> {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const poll = () => {
      try {
        if (fs.existsSync(portFile)) {
          const raw = fs.readFileSync(portFile, "utf-8").trim();
          const port = parseInt(raw, 10);
          if (!Number.isNaN(port) && port > 0) {
            resolve(port);
            return;
          }
        }
      } catch {
        /* keep polling */
      }
      if (Date.now() - start > timeoutMs) {
        reject(new Error("backend did not write its port file in time"));
        return;
      }
      setTimeout(poll, 150);
    };
    poll();
  });
}

function startBackend(): Promise<number> {
  const backendCmd = findBackendCommand();
  if (!backendCmd) {
    return Promise.reject(
      new Error(
        "Could not find a Python interpreter or the bundled backend. " +
        "Install Python 3.10+ or set PHAI_BACKEND.",
      ),
    );
  }
  const portFile = path.join(os.tmpdir(), `phai-port-${process.pid}.txt`);
  try {
    fs.rmSync(portFile, { force: true });
  } catch {
    /* ignore */
  }

  const root = resourceRoot();
  // Stable default port so the browser origin (http://127.0.0.1:<port>) is
  // IDENTICAL across launches — otherwise localStorage (onboarding flag,
  // theme, mic permission) resets on every close/open because it's scoped per
  // origin INCLUDING the port. The backend falls back to a random port only
  // if 47611 is already taken.
  const preferredPort = Number(process.env.PHAI_APP_PORT || 47611) || 47611;
  backend = spawn(
    backendCmd.command,
    [...backendCmd.args, "--port-file", portFile],
    {
      cwd: root,
      env: {
        ...process.env,
        // bind to all interfaces so the iPhone companion can reach the
        // backend over the LAN (http://<PC-IP>:<port>). The Electron window
        // itself still connects via 127.0.0.1. Set PHAI_HOST=127.0.0.1 to
        // force localhost-only if you don't want LAN access.
        PHAI_HOST: process.env.PHAI_HOST || "0.0.0.0",
        PHAI_PORT: String(preferredPort),
        PHAI_APP_VERSION: app.getVersion(),
        PHAI_DATA_DIR: path.join(app.getPath("userData"), "data"),
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  backend.stdout?.on("data", (d: Buffer) => console.log("[backend]", d.toString().trim()));
  backend.stderr?.on("data", (d: Buffer) => console.error("[backend]", d.toString().trim()));
  backend.on("exit", (code) => {
    console.log(`[backend] exited with code ${code}`);
    backend = null;
  });

  return waitForPortFile(portFile);
}

function stopBackend() {
  if (backend && !backend.killed) {
    backend.kill();
  }
  backend = null;
}

// --------------------------------------------------------------------------
// window
// --------------------------------------------------------------------------

function backendUrl(pathname = "") {
  return `http://127.0.0.1:${backendPort}${pathname}`;
}

// --------------------------------------------------------------------------
// system tray presence (Jarvis: the app lives in the tray, not as a window)
// --------------------------------------------------------------------------

function createTray() {
  const iconPath = path.join(resourceRoot(), "build", "icon.png");
  const icon = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath).resize({ width: 22, height: 22 })
    : nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip("Phantom — sleeping");

  const wake = async (agent: string) => {
    try {
      await fetch(backendUrl("/api/presence/wake"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent, source: "tray" }),
      });
      showWindow();
    } catch (e) {
      console.error("[tray] wake failed", e);
    }
  };

  const rebuildMenu = () => {
    tray?.setContextMenu(Menu.buildFromTemplate([
      { label: "Open Phantom", click: showWindow },
      { type: "separator" },
      { label: "👻 Wake Phantom", click: () => wake("phantom") },
      { label: "💻 Wake Coded", click: () => wake("coded") },
      { label: "🔇 Stay silent", click: () => {
          try { fetch(backendUrl("/api/presence/stay-silent"), { method: "POST" }); } catch {}
        } },
      { type: "separator" },
      { label: "⏻ Kill switch", click: () => {
          try { fetch(backendUrl("/api/killswitch/engage"), { method: "POST",
            headers: { "Content-Type": "application/json" }, body: "{}" }); } catch {}
        } },
      { label: "Quit", click: () => { isQuitting = true; app.quit(); } },
    ]));
  };
  rebuildMenu();
  tray.on("click", showWindow);
}

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
}

// live tray presence: poll the backend state and reflect it in the tooltip
function startTrayPresencePolling() {
  const poll = async () => {
    try {
      const res = await fetch(backendUrl("/api/presence"), { signal: AbortSignal.timeout(4000) });
      const p = await res.json();
      const agent = p.active_agent === "coded" ? "Coded" : p.active_agent === "phantom" ? "Phantom" : "";
      const label = p.state === "listening"
        ? `${agent} awake — listening`
        : p.state === "silenced" ? "Staying silent"
        : p.state === "killed" ? "KILL SWITCH ENGAGED"
        : "Sleeping — say Phantom or Coded";
      tray?.setToolTip("Phantom · " + label);
    } catch (e) { /* backend briefly down — keep last tooltip */ }
  };
  poll();
  setInterval(poll, 5000);
}

// auto-start at login (Windows/macOS native; Linux via XDG autostart)
function enableAutoStart() {
  try {
    if (process.platform === "linux") {
      const autostartDir = path.join(os.homedir(), ".config", "autostart");
      fs.mkdirSync(autostartDir, { recursive: true });
      const desktop = [
        "[Desktop Entry]",
        "Type=Application",
        `Name=Phantom`,
        `Exec=${process.execPath} --hidden`,
        "X-GNOME-Autostart-enabled=true",
        "Comment=Phantom personal AI companion",
      ].join("\n");
      fs.writeFileSync(path.join(autostartDir, "phantom.desktop"), desktop);
    } else {
      app.setLoginItemSettings({ openAtLogin: true, openAsHidden: true });
    }
    console.log("[tray] auto-start enabled");
  } catch (e) {
    console.warn("[tray] auto-start failed", e);
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    title: "Phantom",
    backgroundColor: "#0b0e14",
    icon: process.platform === "linux"
      ? path.join(resourceRoot(), "build", "icon.png")
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.setMenuBarVisibility(false);
  if (updateStatus.state !== "idle") {
    mainWindow.webContents.once("did-finish-load", () =>
      mainWindow?.webContents.send("update:status", updateStatus));
  }

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  mainWindow.loadURL(`http://127.0.0.1:${backendPort}`);

  // F12 / Ctrl+Shift+I / Ctrl+Shift+C → developer options (DevTools)
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    const key = String(input.key || "").toLowerCase();
    const devtoolsShortcut = input.key === "F12" ||
      (input.control && input.shift && (key === "i" || key === "c"));
    if (devtoolsShortcut) {
      mainWindow?.webContents.toggleDevTools();
      event.preventDefault();
    }
  });

  // Jarvis behavior: closing the window hides to tray; the agent stays alive.
  mainWindow.on("close", (ev) => {
    if (!isQuitting) {
      ev.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// --------------------------------------------------------------------------
// app lifecycle
// --------------------------------------------------------------------------

function buildAppMenu() {
  const template: MenuItemConstructorOptions[] = [
    { label: "Phantom", submenu: [{ role: "quit" }] },
    { label: "View", submenu: [
      { role: "reload", label: "Reload (Ctrl+R)" },
      { role: "forceReload", label: "Force reload" },
      { type: "separator" },
      { role: "toggleDevTools", label: "Developer options (F12)", accelerator: "F12" },
      { type: "separator" },
      { role: "togglefullscreen" },
    ]},
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  initUpdater();
  buildAppMenu();
  try {
    backendPort = await startBackend();
  } catch (err) {
    dialog.showErrorBox(
      "Phantom — backend failed to start",
      err instanceof Error ? err.message : String(err),
    );
    app.quit();
    return;
  }
  createTray();
  enableAutoStart();
  startTrayPresencePolling();
  // Jarvis: launch into the tray; open the window unless --hidden (autostart).
  if (!process.argv.includes("--hidden")) {
    createWindow();
  }
  app.on("activate", () => showWindow());
});

app.on("window-all-closed", () => {
  // Jarvis: keep running in the tray; quit only via tray "Quit" or kill.
});

app.on("before-quit", () => {
  isQuitting = true;
  stopBackend();
});

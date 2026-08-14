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

import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
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
    "sudo chown root:root /opt/PhantomCoded/chrome-sandbox && sudo chmod 4755 /opt/PhantomCoded/chrome-sandbox",
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
    sendUpdateStatus({ state: "error", message: String(err?.message || err) }));

  ipcMain.handle("update:check", async () => {
    if (!app.isPackaged) {
      return { state: "dev", message: "updates are only available in the packaged app" };
    }
    try {
      const result = await autoUpdater.checkForUpdates();
      return { state: "checking", result: !!result };
    } catch (err) {
      return { state: "error", message: String(err) };
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
  backend = spawn(
    backendCmd.command,
    [...backendCmd.args, "--port-file", portFile],
    {
      cwd: root,
      env: {
        ...process.env,
        PHAI_HOST: "127.0.0.1",
        PHAI_PORT: "0",
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
  mainWindow.on("closed", () => {
    mainWindow = null;
    stopBackend();
    app.quit();
  });
}

// --------------------------------------------------------------------------
// app lifecycle
// --------------------------------------------------------------------------

app.whenReady().then(async () => {
  initUpdater();
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
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", () => {
  stopBackend();
});

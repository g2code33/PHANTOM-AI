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

import { app, BrowserWindow, dialog, shell } from "electron";
import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

let backend: ChildProcess | null = null;
let backendPort = 0;
let mainWindow: BrowserWindow | null = null;

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
  const python = findPython();
  if (!python) {
    return Promise.reject(
      new Error(
        "Could not find a Python interpreter. Install Python 3.10+ or set PHAI_PYTHON.",
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
    python,
    ["-m", "phantom_ai.main", "--port-file", portFile],
    {
      cwd: root,
      env: {
        ...process.env,
        PHAI_HOST: "127.0.0.1",
        PHAI_PORT: "0",
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
    title: "PHANTOM + CODED",
    backgroundColor: "#0b0e14",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.setMenuBarVisibility(false);

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
  try {
    backendPort = await startBackend();
  } catch (err) {
    dialog.showErrorBox(
      "PHANTOM + CODED — backend failed to start",
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

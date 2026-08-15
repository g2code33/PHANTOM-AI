/**
 * PHANTOM + CODED — preload script.
 *
 * Exposes a minimal, safe updater API to the renderer via contextBridge
 * (contextIsolation stays on; the renderer never gets Node access).
 */

import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("phaiUpdater", {
  check: () => ipcRenderer.invoke("update:check"),
  download: () => ipcRenderer.invoke("update:download"),
  install: () => ipcRenderer.invoke("update:install"),
  getVersion: () => ipcRenderer.invoke("update:version"),
  onStatus: (callback: (status: unknown) => void) => {
    const listener = (_event: unknown, status: unknown) => callback(status);
    ipcRenderer.on("update:status", listener);
    return () => ipcRenderer.removeListener("update:status", listener);
  },
});

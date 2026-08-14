#!/usr/bin/env node
/**
 * Web build: PHANTOM + CODED's UI is static (no bundler), so "building" the
 * web app means copying ui/ to dist/ (Capacitor's webDir and electron-builder's
 * "dist" directory expectation).
 */
"use strict";

const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const src = path.join(root, "ui");
const dst = path.join(root, "dist");

fs.rmSync(dst, { recursive: true, force: true });
fs.mkdirSync(dst, { recursive: true });

let count = 0;
for (const name of fs.readdirSync(src)) {
  const from = path.join(src, name);
  const to = path.join(dst, name);
  fs.cpSync(from, to, { recursive: true });
  count++;
}
console.log(`[web:build] copied ${count} item(s) from ui/ → dist/`);

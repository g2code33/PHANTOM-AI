/* Phantom Dev Console — a mini in-app DevTools console.
 *
 * Captures EVERYTHING the real console sees:
 *   - console.log/info/warn/error/debug (teed to the real console too)
 *   - window errors incl. resource-load errors (capture phase)
 *   - unhandled promise rejections
 *   - fetch/network calls (method, url, status, duration, failures)
 *   - WebSocket open/close/error events
 *
 * Plus a REPL: PhantomConsole.evalJs(code) evaluates in the page context,
 * and PhantomConsole.getLogs() feeds Settings -> Diagnostics.
 *
 * Nothing here changes app behavior — it only observes and mirrors.
 */
(function () {
  "use strict";
  var MAX = 600;
  var ring = [];
  function push(entry) { ring.push(entry); if (ring.length > MAX) ring.shift(); }
  function ts() { return new Date().toISOString().slice(11, 23); }
  function fmt(v) {
    try {
      if (typeof v === "string") return v;
      if (v instanceof Error) return v.stack || String(v);
      return JSON.stringify(v);
    } catch (e) { return String(v); }
  }

  // ---- console.* capture (mirror to real console) ----
  var real = {};
  ["log", "info", "warn", "error", "debug"].forEach(function (lvl) {
    real[lvl] = console[lvl] ? console[lvl].bind(console) : function () {};
  });
  function cap(lvl, args) {
    var parts = [];
    for (var i = 0; i < args.length; i++) parts.push(fmt(args[i]));
    push({ t: ts(), level: lvl, text: parts.join(" ") });
  }
  console.log = function () { cap("log", arguments); real.log.apply(null, arguments); };
  console.info = function () { cap("info", arguments); real.info.apply(null, arguments); };
  console.warn = function () { cap("warn", arguments); real.warn.apply(null, arguments); };
  console.error = function () { cap("error", arguments); real.error.apply(null, arguments); };
  console.debug = function () { cap("debug", arguments); real.debug.apply(null, arguments); };

  // ---- window errors (capture phase also catches resource-load errors) ----
  window.addEventListener("error", function (ev) {
    var msg = ev.error ? (ev.error.stack || ev.error.message || String(ev.error)) : ev.message;
    push({ t: ts(), level: "error", text: (ev.filename || "") + ":" + (ev.lineno || "?") + " " + msg });
  }, true);
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev.reason || {};
    push({ t: ts(), level: "error", text: "Unhandled rejection: " + ((r && (r.stack || r.message)) || String(r)) });
  });

  // ---- fetch capture ----
  var realFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || String(input);
    var method = (init && init.method) || (input && input.method) || "GET";
    var start = performance.now();
    return realFetch.apply(this, arguments).then(function (res) {
      push({ t: ts(), level: "net", text: method + " " + url + " -> " + res.status + " (" + Math.round(performance.now() - start) + "ms)" });
      return res;
    }, function (err) {
      push({ t: ts(), level: "error", text: method + " " + url + " FAILED: " + ((err && err.message) || err) + " (" + Math.round(performance.now() - start) + "ms)" });
      throw err;
    });
  };

  // ---- WebSocket capture (keeps instanceof + statics working) ----
  var RealWS = window.WebSocket;
  function WrappedWS() {
    var ws = new (Function.prototype.bind.apply(RealWS, [null].concat(Array.prototype.slice.call(arguments))))();
    var url = arguments[0];
    ws.addEventListener("open", function () { push({ t: ts(), level: "net", text: "WS open " + url }); });
    ws.addEventListener("close", function (e) { push({ t: ts(), level: "net", text: "WS close " + url + " (code " + e.code + ")" }); });
    ws.addEventListener("error", function () { push({ t: ts(), level: "error", text: "WS error " + url }); });
    return ws;
  }
  WrappedWS.prototype = RealWS.prototype;
  Object.getOwnPropertyNames(RealWS).forEach(function (k) {
    try { WrappedWS[k] = RealWS[k]; } catch (e) { /* ignore */ }
  });
  window.WebSocket = WrappedWS;

  // ---- API ----
  var api = {
    getLogs: function () { return ring.slice(); },
    getLogsByLevel: function (lvl) { return ring.filter(function (e) { return e.level === lvl; }); },
    clear: function () { ring.length = 0; },
    evalJs: function (code) {
      try {
        var result = (0, eval)(code);
        return { ok: true, result: fmt(result) };
      } catch (e) { return { ok: false, error: String((e && e.stack) || e) }; }
    },
  };
  window.PhantomConsole = api;
})();

"use strict";

const { app, BrowserWindow, ipcMain } = require("electron");
const dgram = require("dgram");
const path = require("path");
const readline = require("readline");
const { fileURLToPath } = require("url");
const { acceptEffectiveSnapshot } = require("./snapshot_contract.cjs");
const { WindowRegistry } = require("./window_registry.cjs");

const registry = new WindowRegistry();
const waiters = new Map();
const latestEvents = new Map();
const catalogRoot = path.resolve(process.env.CODEX_PRESENCE_CATALOG || "");
const udpPort = Number(process.env.CODEX_PRESENCE_UDP_PORT || 17839);
let udp = null;
let quitting = false;

function emit(document) {
  process.stdout.write(JSON.stringify(document) + "\n");
}

function waiterKey(webContentsId, kind, revision = "") {
  return [webContentsId, kind, revision].join(":");
}

function waitFor(webContentsId, kind, revision = "", timeoutMs = 15000) {
  const key = waiterKey(webContentsId, kind, revision);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters.delete(key);
      reject(new Error("renderer acknowledgement timed out"));
    }, timeoutMs);
    waiters.set(key, {
      resolve(value) {
        clearTimeout(timer);
        waiters.delete(key);
        resolve(value);
      },
      reject(error) {
        clearTimeout(timer);
        waiters.delete(key);
        reject(error);
      },
    });
  });
}

function resolveWaiter(senderId, kind, revision, value, error = null) {
  const key = waiterKey(senderId, kind, revision);
  const waiter = waiters.get(key);
  if (!waiter) return;
  if (error) waiter.reject(new Error(error));
  else waiter.resolve(value);
}

ipcMain.on("presence-renderer-ready", (event) => {
  resolveWaiter(event.sender.id, "ready", "", true);
});
ipcMain.on("presence-renderer-failed", (event, payload) => {
  resolveWaiter(event.sender.id, "ready", "", null, payload?.error || "renderer failed");
});
ipcMain.on("presence-snapshot-applied", (event, payload) => {
  resolveWaiter(event.sender.id, "snapshot", payload?.revision, payload);
});
ipcMain.on("presence-snapshot-failed", (event, payload) => {
  resolveWaiter(
    event.sender.id,
    "snapshot",
    payload?.revision,
    null,
    payload?.error || "snapshot failed",
  );
});

function safeLive2dUrl(resource) {
  if (!resource || resource.kind !== "live2d" || typeof resource.url !== "string") {
    throw new Error("resolved Live2D resource URL is required");
  }
  const filename = path.resolve(fileURLToPath(resource.url));
  const rootPrefix = catalogRoot.endsWith(path.sep) ? catalogRoot : catalogRoot + path.sep;
  if (!catalogRoot || !filename.startsWith(rootPrefix)) {
    throw new Error("Live2D renderer URL is outside the catalog");
  }
  return resource.url;
}

function geometryFor(command, previous) {
  if (previous?.window && !previous.window.isDestroyed()) {
    return previous.window.getBounds();
  }
  const geometry = command.geometry || {};
  return {
    x: Number.isFinite(geometry.x) ? Math.round(geometry.x) : undefined,
    y: Number.isFinite(geometry.y) ? Math.round(geometry.y) : undefined,
    width: Number.isFinite(geometry.width) ? Math.max(160, Math.round(geometry.width)) : 420,
    height: Number.isFinite(geometry.height) ? Math.max(160, Math.round(geometry.height)) : 640,
  };
}

function rendererKey(snapshot, resource) {
  return [snapshot.renderer.kind, snapshot.avatar_ref, resource?.url || "builtin"].join("|");
}

async function applyToWindow(record, snapshot) {
  const accepted = acceptEffectiveSnapshot(snapshot);
  const applied = waitFor(record.window.webContents.id, "snapshot", accepted.revision);
  record.window.webContents.send("presence-snapshot", accepted);
  await applied;
  record.snapshot = accepted;
  record.revision = accepted.revision;
}

async function createReplacement(command, previous) {
  const snapshot = acceptEffectiveSnapshot(command.snapshot);
  const geometry = geometryFor(command, previous);
  const window = new BrowserWindow({
    ...geometry,
    show: false,
    transparent: true,
    frame: false,
    resizable: true,
    hasShadow: false,
    backgroundColor: "#00000000",
    skipTaskbar: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  const record = {
    bindingId: snapshot.binding_id,
    key: rendererKey(snapshot, command.resource),
    window,
    snapshot: null,
    revision: 0,
    active: command.active !== false,
    ready: false,
    destroy() {
      if (!window.isDestroyed()) window.destroy();
    },
  };
  const ready = waitFor(window.webContents.id, "ready");
  if (snapshot.renderer.kind === "builtin") {
    await window.loadFile(path.join(__dirname, "window", "index.html"));
  } else {
    await window.loadURL(safeLive2dUrl(command.resource));
  }
  await ready;
  await applyToWindow(record, snapshot);
  record.ready = true;

  const state = latestEvents.get(snapshot.binding_id);
  if (state?.activity) window.webContents.send("presence-event", state.activity);
  if (state?.playback) window.webContents.send("presence-event", state.playback);
  if (record.active && snapshot.renderer.visible) window.showInactive();

  const reportGeometry = () => {
    if (window.isDestroyed()) return;
    emit({
      type: "renderer/geometry",
      binding_id: snapshot.binding_id,
      geometry: window.getBounds(),
    });
  };
  window.on("move", reportGeometry);
  window.on("resize", reportGeometry);
  return record;
}

async function applySnapshot(command) {
  const snapshot = acceptEffectiveSnapshot(command.snapshot);
  const current = registry.current(snapshot.binding_id);
  const key = rendererKey(snapshot, command.resource);
  if (current && current.key === key && !current.window.isDestroyed()) {
    await applyToWindow(current, snapshot);
    current.active = command.active !== false;
    if (current.active && snapshot.renderer.visible) current.window.showInactive();
    else current.window.hide();
    return current;
  }
  return registry.swap(
    snapshot.binding_id,
    () => createReplacement(command, current),
  );
}

function routeEvent(event) {
  if (!event || typeof event.binding_id !== "string") return false;
  const record = registry.current(event.binding_id);
  if (!record || record.window.isDestroyed()) return false;
  const state = latestEvents.get(event.binding_id) || {};
  if (event.type === "activity") state.activity = event;
  if (event.type === "state" || event.type === "voice-output") state.playback = event;
  latestEvents.set(event.binding_id, state);
  record.window.webContents.send("presence-event", event);
  return true;
}

async function handle(command) {
  if (!command || typeof command !== "object") throw new Error("command must be an object");
  if (command.type === "snapshot") {
    const record = await applySnapshot(command);
    return {
      binding_id: record.bindingId,
      revision: record.revision,
      renderer_key: record.key,
    };
  }
  if (command.type === "event") {
    if (!command.event || typeof command.event.utterance_id !== "string") {
      throw new Error("renderer event requires an utterance_id");
    }
    return { routed: routeEvent(command.event) };
  }
  if (command.type === "activity") {
    return { routed: routeEvent(command.event) };
  }
  if (command.type === "binding-state") {
    const record = registry.current(command.binding_id);
    if (!record) return { found: false };
    record.active = Boolean(command.active);
    if (record.active && record.snapshot?.renderer.visible) record.window.showInactive();
    else record.window.hide();
    return { found: true, active: record.active };
  }
  if (command.type === "remove") {
    latestEvents.delete(command.binding_id);
    return { removed: registry.remove(command.binding_id) };
  }
  if (command.type === "status") {
    const records = [...registry.records.values()].map((record) => ({
      binding_id: record.bindingId,
      revision: record.revision,
      renderer_key: record.key,
      active: record.active,
      visible: !record.window.isDestroyed() && record.window.isVisible(),
    }));
    return { root_pid: process.pid, udp_port: udpPort, windows: records };
  }
  if (command.type === "shutdown") {
    quitting = true;
    registry.closeAll();
    if (udp) udp.close();
    setImmediate(() => app.quit());
    return { shutting_down: true };
  }
  throw new Error("unsupported renderer command: " + command.type);
}

function startUdp() {
  udp = dgram.createSocket("udp4");
  udp.on("message", (buffer) => {
    try {
      const event = JSON.parse(buffer.toString("utf8"));
      if (
        typeof event.binding_id !== "string"
        || typeof event.utterance_id !== "string"
        || !["audio", "state", "voice-output"].includes(event.type)
      ) return;
      routeEvent(event);
    } catch (_) {
      // High-frequency malformed packets are dropped without touching a sibling.
    }
  });
  udp.bind(udpPort, "127.0.0.1", () => {
    emit({ type: "renderer/ready", root_pid: process.pid, udp_port: udpPort });
  });
}

app.on("window-all-closed", (event) => {
  event.preventDefault();
});

app.whenReady().then(() => {
  startUdp();
  const lines = readline.createInterface({ input: process.stdin });
  lines.on("line", async (line) => {
    let command;
    try {
      command = JSON.parse(line);
      const result = await handle(command);
      emit({ type: "response", id: command.id, ok: true, result });
    } catch (error) {
      emit({
        type: "response",
        id: command?.id,
        ok: false,
        error: String(error?.message || error),
      });
    }
  });
  lines.on("close", () => {
    if (!quitting) {
      registry.closeAll();
      if (udp) udp.close();
      app.quit();
    }
  });
});


"use strict";

const { app, BrowserWindow, ipcMain } = require("electron");
const dgram = require("dgram");
const fs = require("fs");
const crypto = require("crypto");
const path = require("path");
const readline = require("readline");
const net = require("net");
const { fileURLToPath } = require("url");
const { acceptEffectiveSnapshot } = require("./snapshot_contract.cjs");
const { WindowRegistry } = require("./window_registry.cjs");
const { interactionBounds } = require("./interaction_geometry.cjs");

const registry = new WindowRegistry();
const waiters = new Map();
const latestEvents = new Map();
const catalogRoot = path.resolve(process.env.CODEX_PRESENCE_CATALOG || "");
let udpPort = Number(process.env.CODEX_PRESENCE_UDP_PORT || 17839);
let udp = null;
let controlSocket = null;
let quitting = false;
let inputEnabled = process.env.CODEX_PRESENCE_INPUT_ENABLED === "1";
const inputRoot = path.resolve(process.env.CODEX_PRESENCE_INPUT_ROOT || "");
const captures = new Map();
const interactions = new Map();
const MAX_RECORDING_BYTES = 25 * 1024 * 1024;

function emit(document) {
  const line = JSON.stringify(document) + "\n";
  if (controlSocket?.writable) controlSocket.write(line);
  else process.stdout.write(line);
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

function recordForSender(senderId) {
  for (const record of registry.records.values()) {
    if (!record.window.isDestroyed() && record.window.webContents.id === senderId) return record;
  }
  return null;
}

function safeCapturePath(captureId) {
  if (!inputRoot || !/^[a-zA-Z0-9_-]{1,80}$/.test(captureId)) {
    throw new Error("voice input capture id or root is invalid");
  }
  const destination = path.resolve(inputRoot, `${captureId}.webm`);
  const prefix = inputRoot.endsWith(path.sep) ? inputRoot : inputRoot + path.sep;
  if (!destination.startsWith(prefix)) throw new Error("voice input recording escaped its root");
  return destination;
}

function emitCaptureCancel(capture, reason) {
  if (!capture) return;
  emit({
    type: "renderer/input",
    state: "capture-cancel",
    binding_id: capture.bindingId,
    capture_id: capture.captureId,
    reason: String(reason || "capture cancelled"),
  });
}

function cancelCaptureForSender(senderId, reason) {
  const capture = captures.get(senderId);
  captures.delete(senderId);
  emitCaptureCancel(capture, reason);
  return Boolean(capture);
}

ipcMain.handle("presence-input-config", (event) => {
  const record = recordForSender(event.sender.id);
  return {
    enabled: Boolean(
      inputEnabled && record && record.inputAllowed && record.snapshot?.renderer?.visible
    ),
    gesture: "hold-ctrl-alt-right",
    binding_id: record?.bindingId || null,
  };
});

ipcMain.handle("presence-input-start", (event, payload) => {
  const record = recordForSender(event.sender.id);
  if (
    !inputEnabled
    || !record
    || !record.inputAllowed
    || !record.active
    || !record.snapshot?.renderer?.visible
  ) return { ok: false, error: "voice_input_disabled" };
  if (captures.has(event.sender.id)) return { ok: false, error: "capture_already_active" };
  const captureId = String(payload?.capture_id || crypto.randomUUID());
  safeCapturePath(captureId);
  captures.set(event.sender.id, { captureId, bindingId: record.bindingId });
  emit({
    type: "renderer/input",
    state: "capture-start",
    binding_id: record.bindingId,
    capture_id: captureId,
  });
  return { ok: true, capture_id: captureId, binding_id: record.bindingId };
});

ipcMain.handle("presence-input-finish", async (event, payload) => {
  const capture = captures.get(event.sender.id);
  captures.delete(event.sender.id);
  if (!capture || payload?.capture_id !== capture.captureId) {
    emitCaptureCancel(capture, "capture identity mismatch");
    return { ok: false, error: "capture_identity_mismatch" };
  }
  try {
    const bytes = Buffer.from(payload?.bytes || []);
    if (!bytes.length || bytes.length > MAX_RECORDING_BYTES) {
      emitCaptureCancel(capture, "recording size invalid");
      return { ok: false, error: "recording_size_invalid" };
    }
    const destination = safeCapturePath(capture.captureId);
    fs.mkdirSync(inputRoot, { recursive: true });
    const temporary = destination + ".tmp";
    await fs.promises.writeFile(temporary, bytes);
    await fs.promises.rename(temporary, destination);
    emit({
      type: "renderer/input",
      state: "capture-finish",
      binding_id: capture.bindingId,
      capture_id: capture.captureId,
      recording: destination,
    });
    return { ok: true };
  } catch (error) {
    emitCaptureCancel(capture, error?.message || error);
    throw error;
  }
});

ipcMain.handle("presence-input-cancel", (event) => {
  cancelCaptureForSender(event.sender.id, "renderer cancelled capture");
  return { ok: true };
});

ipcMain.on("presence-window-interaction", (event, payload) => {
  const record = recordForSender(event.sender.id);
  if (!record || record.window.isDestroyed()) return;
  const phase = payload?.phase;
  if (phase === "start") {
    const mode = payload?.mode;
    const point = { x: Number(payload?.x), y: Number(payload?.y) };
    try {
      interactionBounds(record.window.getBounds(), point, point, mode);
    } catch (_) {
      return;
    }
    interactions.set(event.sender.id, {
      mode,
      point,
      bounds: record.window.getBounds(),
    });
    return;
  }
  if (phase === "end") {
    interactions.delete(event.sender.id);
    return;
  }
  if (phase !== "update") return;
  const interaction = interactions.get(event.sender.id);
  if (!interaction) return;
  try {
    const bounds = interactionBounds(
      interaction.bounds,
      interaction.point,
      { x: Number(payload?.x), y: Number(payload?.y) },
      interaction.mode,
    );
    record.window.setBounds(bounds, false);
  } catch (_) {
    interactions.delete(event.sender.id);
  }
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
    inputAllowed: command.input_allowed === true,
    ready: false,
    destroy() {
      if (!window.isDestroyed()) window.destroy();
    },
  };
  const webContentsId = window.webContents.id;
  try {
    const ready = waitFor(webContentsId, "ready");
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
    window.on("closed", () => {
      interactions.delete(webContentsId);
      cancelCaptureForSender(webContentsId, "renderer window closed");
    });
    return record;
  } catch (error) {
    record.destroy();
    throw error;
  }
}

async function applySnapshot(command) {
  const snapshot = acceptEffectiveSnapshot(command.snapshot);
  const current = registry.current(snapshot.binding_id);
  const key = rendererKey(snapshot, command.resource);
  if (current && current.key === key && !current.window.isDestroyed()) {
    await applyToWindow(current, snapshot);
    current.active = command.active !== false;
    current.inputAllowed = command.input_allowed === true;
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
  if (command.type === "input-policy") {
    inputEnabled = Boolean(command.enabled);
    for (const record of registry.records.values()) {
      if (!record.window.isDestroyed()) {
        record.window.webContents.send("presence-input-policy", { enabled: inputEnabled });
      }
    }
    return { enabled: inputEnabled };
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
  udp.once("error", (error) => {
    emit({ type: "renderer/error", error: String(error?.message || error) });
    app.quit();
  });
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
    udpPort = Number(udp.address().port);
    emit({ type: "renderer/ready", root_pid: process.pid, udp_port: udpPort });
  });
}

function bindCommands(input) {
  const lines = readline.createInterface({ input });
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
}

app.on("window-all-closed", () => {});

app.whenReady().then(() => {
  const controlPort = Number(process.env.CODEX_PRESENCE_CONTROL_PORT || 0);
  const controlToken = String(process.env.CODEX_PRESENCE_CONTROL_TOKEN || "");
  if (controlPort > 0 && controlToken) {
    controlSocket = net.createConnection(
      { host: "127.0.0.1", port: controlPort },
      () => {
        controlSocket.setNoDelay(true);
        controlSocket.write(JSON.stringify({ type: "renderer/auth", token: controlToken }) + "\n");
        bindCommands(controlSocket);
        startUdp();
      },
    );
    controlSocket.once("error", (error) => {
      console.error(`renderer control connection failed: ${error?.message || error}`);
      app.quit();
    });
    return;
  }
  bindCommands(process.stdin);
  startUdp();
});

const { app, BrowserWindow, ipcMain, screen, session } = require("electron");
const { spawnSync } = require("node:child_process");
const dgram = require("node:dgram");
const fs = require("node:fs");
const path = require("node:path");

const PORT = Number(process.env.CODEX_ORB_PORT || 17831);
const SIZE = 440;
const MIN_SIZE = 220;
const DEFAULT_MARGIN = 36;
const PID_PATH = path.join(__dirname, "orb.pid");
const LOG_PATH = path.join(__dirname, "orb.log");
const POSITION_PATH = path.join(__dirname, "..", "orb-position.json");
const VOICE_ROOT = path.join(__dirname, "..");
const PROJECT_ROOT = path.join(VOICE_ROOT, "..");
const AVATAR_SOURCE_ROOT = path.join(PROJECT_ROOT, ".codex-voice-avatars");
const AVATAR_SELECTION_PATH = path.join(VOICE_ROOT, "avatar-selection.json");
const AVATAR_STATE_PATH = path.join(VOICE_ROOT, "avatar-state.json");
const AVATAR_STATUS_PATH = path.join(VOICE_ROOT, "avatar-state-status.json");
const INPUT_SETTINGS_PATH = path.join(VOICE_ROOT, "input.json");
const INPUT_RECORDINGS_ROOT = path.join(VOICE_ROOT, "inbox", "recordings");
const AVATAR_STATE_SCHEMA = "codex-ai-presence/avatar-state/v0.1";
const AVATAR_STATE_CAPABILITY = "avatar-state-v1";
const ACTION_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const SOURCE_PATTERN = /^[A-Za-z0-9._:-]{1,80}$/;
const MAX_ACTIONS = 128;
let windowRef = null;
let socket = null;
let moveMode = false;
let dragState = null;
let rendererReady = false;
let avatarStateWatcher = false;
let activeAvatar = null;
let acceptedAvatarState = null;
const lastAvatarRevisions = new Map();
let windowStateWriteTimer = null;
let resizeState = null;

function inputSettings() {
  try {
    const value = JSON.parse(fs.readFileSync(INPUT_SETTINGS_PATH, "utf8"));
    return value && typeof value === "object" ? value : {};
  } catch (_) {
    return {};
  }
}

function inputEnabled() {
  return inputSettings().input_enabled === true;
}

function runVoiceInput(args) {
  const python = process.platform === "win32"
    ? path.join(VOICE_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(VOICE_ROOT, ".venv", "bin", "python");
  const script = path.join(VOICE_ROOT, "voice_input.py");
  if (!fs.existsSync(python) || !fs.existsSync(script)) {
    return { ok: false, error: "voice_input_runtime_missing" };
  }
  const result = spawnSync(python, [script, "--voice-root", VOICE_ROOT, ...args], {
    cwd: PROJECT_ROOT,
    encoding: "utf8",
    windowsHide: true,
    timeout: 5000,
  });
  const lines = String(result.stdout || "").trim().split(/\r?\n/).filter(Boolean);
  try {
    return lines.length ? JSON.parse(lines[lines.length - 1]) : { ok: false, error: "voice_input_no_response" };
  } catch (_) {
    return { ok: false, error: String(result.stderr || "voice_input_failed").trim() };
  }
}

function recordingPath(recordingId) {
  if (typeof recordingId !== "string" || !/^[a-zA-Z0-9_-]{1,80}$/.test(recordingId)) {
    throw new Error("invalid recording id");
  }
  fs.mkdirSync(INPUT_RECORDINGS_ROOT, { recursive: true });
  const candidate = path.resolve(INPUT_RECORDINGS_ROOT, `${recordingId}.webm`);
  if (!isWithin(path.resolve(INPUT_RECORDINGS_ROOT), candidate)) {
    throw new Error("recording escaped the input boundary");
  }
  return candidate;
}

function log(message) {
  try {
    fs.appendFileSync(LOG_PATH, `${new Date().toISOString()} ${message}\n`, "utf8");
  } catch (_) {
    // Diagnostics must never prevent the orb from running.
  }
}

function writePid() {
  fs.writeFileSync(PID_PATH, `${process.pid}\n`, "utf8");
}

function removePid() {
  try {
    if (fs.readFileSync(PID_PATH, "utf8").trim() === String(process.pid)) {
      fs.unlinkSync(PID_PATH);
    }
  } catch (_) {
    // The process is already exiting; there is nothing useful to report.
  }
}

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function builtinEntry() {
  return path.join(__dirname, "index.html");
}

function builtinAvatarInfo() {
  return {
    entry: builtinEntry(),
    id: "builtin",
    capabilities: [],
    bundleRoot: null,
    stateSupported: false,
  };
}

function selectedAvatarInfo() {
  try {
    const selection = JSON.parse(fs.readFileSync(AVATAR_SELECTION_PATH, "utf8"));
    const avatarId = typeof selection.avatar_id === "string" ? selection.avatar_id : "";
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(avatarId) || avatarId === "builtin") {
      return builtinAvatarInfo();
    }
    const bundleRoot = path.resolve(AVATAR_SOURCE_ROOT, avatarId);
    if (!isWithin(path.resolve(AVATAR_SOURCE_ROOT), bundleRoot)) {
      throw new Error("avatar bundle escaped the source root");
    }
    const manifestPath = path.join(bundleRoot, "avatar.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    if (manifest.schema !== "codex-ai-presence/avatar/v0.1" || manifest.id !== avatarId) {
      throw new Error("avatar manifest schema or id mismatch");
    }
    const entry = typeof manifest.entry === "string" ? manifest.entry : "";
    if (!entry || entry.startsWith("/") || entry.startsWith("\\") || entry.includes(":") || entry.includes("..")) {
      throw new Error("avatar entry must be a relative path");
    }
    const entryPath = path.resolve(bundleRoot, entry);
    if (!entryPath.toLowerCase().endsWith(".html") || !isWithin(bundleRoot, entryPath) || !fs.existsSync(entryPath) || !fs.statSync(entryPath).isFile()) {
      throw new Error("avatar entry is missing or outside the bundle");
    }
    const capabilities = Array.isArray(manifest.capabilities)
      ? manifest.capabilities.filter((value) => typeof value === "string")
      : [];
    const capabilityPath = path.join(bundleRoot, "avatar-capabilities.json");
    return {
      entry: entryPath,
      id: avatarId,
      capabilities,
      bundleRoot,
      stateSupported: capabilities.includes(AVATAR_STATE_CAPABILITY) && fs.existsSync(capabilityPath),
    };
  } catch (error) {
    log(`avatar fallback: ${error.message}`);
    return builtinAvatarInfo();
  }
}

function writeAvatarStatus(reason, state, accepted) {
  const status = {
    schema: "codex-ai-presence/avatar-state-status/v0.1",
    type: "avatar-state-status",
    avatar_id: activeAvatar?.id || "builtin",
    accepted: Boolean(accepted),
    reason,
    revision: Number.isInteger(state?.revision) ? state.revision : null,
    action_count: Array.isArray(state?.actions) ? state.actions.length : 0,
    updated_at: new Date().toISOString(),
  };
  try {
    fs.writeFileSync(AVATAR_STATUS_PATH, `${JSON.stringify(status, null, 2)}\n`, "utf8");
  } catch (error) {
    log(`avatar status write failed: ${error.message}`);
  }
}

function parseAvatarState(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("avatar state must be a JSON object");
  }
  if (value.schema !== AVATAR_STATE_SCHEMA || value.type !== "avatar-state") {
    throw new Error("unsupported avatar state schema");
  }
  if (typeof value.avatar_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.avatar_id)) {
    throw new Error("avatar state has an invalid avatar_id");
  }
  if (typeof value.source !== "string" || !SOURCE_PATTERN.test(value.source)) {
    throw new Error("avatar state has an invalid source");
  }
  if (value.scope !== "project") {
    throw new Error("avatar state scope must be project");
  }
  if (typeof value.issued_at !== "string" || !value.issued_at || Number.isNaN(Date.parse(value.issued_at))) {
    throw new Error("avatar state issued_at must be an ISO-8601 timestamp");
  }
  if (!Number.isInteger(value.revision) || value.revision < 0) {
    throw new Error("avatar state revision must be a non-negative integer");
  }
  if (!Array.isArray(value.actions) || value.actions.length > MAX_ACTIONS) {
    throw new Error("avatar state actions must be an array of at most 128 items");
  }
  const actions = [];
  for (const action of value.actions) {
    if (typeof action !== "string" || !ACTION_ID_PATTERN.test(action)) {
      throw new Error("avatar state contains an invalid action id");
    }
    if (actions.includes(action)) {
      throw new Error("avatar state contains duplicate action ids");
    }
    actions.push(action);
  }
  return { ...value, actions: actions.sort() };
}

function sendAvatarState() {
  if (!rendererReady || !windowRef || windowRef.isDestroyed() || !acceptedAvatarState) {
    return;
  }
  windowRef.webContents.send("avatar-state", acceptedAvatarState);
}

function loadAvatarState() {
  let raw;
  try {
    if (fs.statSync(AVATAR_STATE_PATH).size > 64 * 1024) {
      throw new Error("avatar state exceeds the 64 KiB limit");
    }
    raw = JSON.parse(fs.readFileSync(AVATAR_STATE_PATH, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") {
      writeAvatarStatus("missing", null, false);
      return;
    }
    writeAvatarStatus("invalid-json", null, false);
    log(`avatar state read failed: ${error.message}`);
    return;
  }

  let state;
  try {
    state = parseAvatarState(raw);
  } catch (error) {
    writeAvatarStatus("invalid-state", null, false);
    log(`avatar state rejected: ${error.message}`);
    return;
  }
  if (!activeAvatar || activeAvatar.id === "builtin" || !activeAvatar.stateSupported) {
    writeAvatarStatus("unsupported-capability", state, false);
    return;
  }
  if (state.avatar_id !== activeAvatar.id) {
    writeAvatarStatus("avatar-mismatch", state, false);
    return;
  }
  const lastRevision = lastAvatarRevisions.get(state.source);
  if (lastRevision !== undefined && state.revision <= lastRevision) {
    if (state.revision === lastRevision && acceptedAvatarState?.source === state.source) {
      writeAvatarStatus("accepted", acceptedAvatarState, true);
      return;
    }
    writeAvatarStatus("stale-revision", state, false);
    return;
  }
  lastAvatarRevisions.set(state.source, state.revision);
  acceptedAvatarState = state;
  writeAvatarStatus("accepted", state, true);
  sendAvatarState();
}

function startAvatarStateWatcher() {
  if (avatarStateWatcher) {
    return;
  }
  avatarStateWatcher = true;
  fs.watchFile(AVATAR_STATE_PATH, { persistent: false, interval: 200 }, loadAvatarState);
  loadAvatarState();
}

function stopAvatarStateWatcher() {
  if (!avatarStateWatcher) {
    return;
  }
  fs.unwatchFile(AVATAR_STATE_PATH, loadAvatarState);
  avatarStateWatcher = false;
}

function normalizeWindowDimension(value) {
  const dimension = Number(value);
  return Number.isFinite(dimension) ? Math.max(MIN_SIZE, Math.round(dimension)) : SIZE;
}

function clampPosition(x, y, workArea, width = SIZE, height = SIZE) {
  const maxX = Math.max(workArea.x, workArea.x + workArea.width - width);
  const maxY = Math.max(workArea.y, workArea.y + workArea.height - height);
  return {
    x: Math.max(workArea.x, Math.min(Math.round(x), maxX)),
    y: Math.max(workArea.y, Math.min(Math.round(y), maxY)),
  };
}

function readSavedWindowState() {
  try {
    const value = JSON.parse(fs.readFileSync(POSITION_PATH, "utf8"));
    if (Number.isFinite(value.x) && Number.isFinite(value.y)) {
      return {
        x: value.x,
        y: value.y,
        width: normalizeWindowDimension(value.width),
        height: normalizeWindowDimension(value.height),
      };
    }
  } catch (_) {
    // Missing or malformed position state falls back to the default location.
  }
  return null;
}

function writeWindowState(x, y, width, height) {
  try {
    const temporary = `${POSITION_PATH}.tmp`;
    fs.writeFileSync(
      temporary,
      `${JSON.stringify(
        {
          version: 2,
          x,
          y,
          width: normalizeWindowDimension(width),
          height: normalizeWindowDimension(height),
          updatedAt: new Date().toISOString(),
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
    fs.copyFileSync(temporary, POSITION_PATH);
    fs.unlinkSync(temporary);
  } catch (error) {
    log(`position write failed: ${error.message}`);
  }
}

function writeCurrentWindowState() {
  if (!windowRef || windowRef.isDestroyed()) {
    return;
  }
  const [x, y] = windowRef.getPosition();
  const [width, height] = windowRef.getSize();
  writeWindowState(x, y, width, height);
}

function scheduleWindowStateWrite() {
  if (windowStateWriteTimer !== null) {
    clearTimeout(windowStateWriteTimer);
  }
  windowStateWriteTimer = setTimeout(() => {
    windowStateWriteTimer = null;
    writeCurrentWindowState();
  }, 120);
}

function startupPosition(primaryDisplay) {
  const saved = readSavedWindowState();
  const width = saved?.width || SIZE;
  const height = saved?.height || SIZE;
  if (saved) {
    const display = screen.getDisplayNearestPoint(saved);
    const position = clampPosition(saved.x, saved.y, display.workArea, width, height);
    return { ...position, width, height };
  }
  const workArea = primaryDisplay.workArea;
  const position = clampPosition(
    workArea.x + workArea.width - width - DEFAULT_MARGIN,
    workArea.y + workArea.height - height - DEFAULT_MARGIN,
    workArea,
    width,
    height,
  );
  return { ...position, width, height };
}

function setMoveMode(enabled) {
  moveMode = Boolean(enabled);
  dragState = null;
  if (windowRef && !windowRef.isDestroyed()) {
    windowRef.setIgnoreMouseEvents(!moveMode, { forward: true });
    windowRef.webContents.send("move-mode", moveMode);
  }
  log(`move mode: ${moveMode ? "on" : "off"}`);
}

function pointFromPayload(payload) {
  const x = Number(payload && payload.screenX);
  const y = Number(payload && payload.screenY);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

function startAudioSocket() {
  socket = dgram.createSocket("udp4");
  socket.on("error", () => {
    // The orb remains usable if the local audio bridge is unavailable.
    log("audio socket error");
  });
  socket.on("message", (message) => {
    try {
      const event = JSON.parse(message.toString("utf8"));
      if (windowRef && !windowRef.isDestroyed()) {
        windowRef.webContents.send("audio-event", event);
      }
    } catch (_) {
      // Ignore malformed local packets.
    }
  });
  socket.bind(PORT, "127.0.0.1");
}

function syncRendererScale() {
  if (!rendererReady || !windowRef || windowRef.isDestroyed()) {
    return;
  }
  const [width, height] = windowRef.getContentSize();
  const baseDimension = Math.min(width, height);
  const rendererKeepsNativeScale = activeAvatar?.id === "builtin" || activeAvatar?.stateSupported;
  const scale = rendererKeepsNativeScale
    ? 1
    : Math.max(0.25, baseDimension / SIZE);
  try {
    windowRef.webContents.setZoomFactor(scale);
    windowRef.webContents.send("window-resize", { width, height });
  } catch (error) {
    log(`renderer scale update failed: ${error.message}`);
  }
}

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const windowState = startupPosition(display);
  activeAvatar = selectedAvatarInfo();
  log(`avatar selected: ${activeAvatar.id}; state bridge: ${activeAvatar.stateSupported ? "supported" : "disabled"}`);
  startAvatarStateWatcher();

  windowRef = new BrowserWindow({
    width: windowState.width,
    height: windowState.height,
    x: windowState.x,
    y: windowState.y,
    minWidth: MIN_SIZE,
    minHeight: MIN_SIZE,
    frame: false,
    transparent: true,
    resizable: true,
    movable: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    show: false,
    backgroundColor: "#00000000",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });

  windowRef.setAlwaysOnTop(true, "floating");
  windowRef.setIgnoreMouseEvents(true, { forward: true });
  windowRef.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    log(`renderer console level=${level} ${sourceId}:${line} ${message}`);
  });
  windowRef.webContents.on("context-menu", (event) => {
    event.preventDefault();
  });
  windowRef.webContents.on("render-process-gone", (_event, details) => {
    log(`renderer gone reason=${details.reason} exitCode=${details.exitCode}`);
  });
  windowRef.webContents.on("did-fail-load", (_event, code, description) => {
    log(`load failed code=${code} ${description}`);
  });
  log(`renderer entry: ${activeAvatar.entry}`);
  windowRef.loadFile(activeAvatar.entry);
  windowRef.webContents.once("did-finish-load", () => {
    log("renderer loaded");
    rendererReady = true;
    syncRendererScale();
    windowRef.webContents.send("move-mode", moveMode);
    sendAvatarState();
    windowRef.showInactive();
  });
  windowRef.on("resize", () => {
    syncRendererScale();
    scheduleWindowStateWrite();
  });
  windowRef.on("closed", () => {
    if (windowStateWriteTimer !== null) {
      clearTimeout(windowStateWriteTimer);
      windowStateWriteTimer = null;
    }
    dragState = null;
    resizeState = null;
    rendererReady = false;
    windowRef = null;
  });
}

ipcMain.on("close-orb", () => app.quit());
ipcMain.on("set-move-mode", (_event, enabled) => setMoveMode(enabled));
ipcMain.handle("voice-input-config", () => inputSettings());
ipcMain.handle("voice-record-start", () => {
  if (!inputEnabled()) {
    return { ok: false, error: "voice_input_disabled" };
  }
  const result = runVoiceInput(["control", "capture-start"]);
  if (windowRef && !windowRef.isDestroyed() && result.ok) {
    windowRef.webContents.send("voice-input-state", {
      state: "listening",
      session_id: result.target_session_id,
      capture_sequence: result.capture_sequence,
    });
  } else if (windowRef && !windowRef.isDestroyed() && !result.ok) {
    windowRef.webContents.send("voice-input-state", { state: "error", error: result.error || "voice_input_failed" });
  }
  return result;
});
ipcMain.handle("voice-record-finish", (_event, payload) => {
  if (!inputEnabled() || !payload || typeof payload !== "object") {
    return { ok: false, error: "voice_input_disabled" };
  }
  try {
    const bytes = payload.bytes;
    if (!bytes || bytes.length > 10 * 1024 * 1024) {
      return { ok: false, error: "recording_too_large" };
    }
    const captureSequence = Number(payload.capture_sequence);
    if (!Number.isInteger(captureSequence) || captureSequence < 1) {
      return { ok: false, error: "capture_sequence_missing" };
    }
    const destination = recordingPath(payload.recording_id);
    fs.writeFileSync(destination, Buffer.from(bytes));
    const result = runVoiceInput([
      "control",
      "capture-finish",
      "--recording",
      destination,
      "--capture-sequence",
      String(captureSequence),
    ]);
    if (!result.ok) {
      fs.rmSync(destination, { force: true });
    }
    return result;
  } catch (error) {
    return { ok: false, error: error.message };
  }
});
ipcMain.handle("voice-record-cancel", () => runVoiceInput(["control", "capture-cancel"]));
ipcMain.on("orb-drag-start", (_event, payload) => {
  if (!moveMode || !windowRef || windowRef.isDestroyed()) {
    return;
  }
  const point = pointFromPayload(payload);
  if (!point) {
    return;
  }
  const [x, y] = windowRef.getPosition();
  dragState = { offsetX: point.x - x, offsetY: point.y - y };
});
ipcMain.on("orb-drag", (_event, payload) => {
  if (!moveMode || !dragState || !windowRef || windowRef.isDestroyed()) {
    return;
  }
  const point = pointFromPayload(payload);
  if (!point) {
    return;
  }
  const display = screen.getDisplayNearestPoint(point);
  const [width, height] = windowRef.getSize();
  const position = clampPosition(
    point.x - dragState.offsetX,
    point.y - dragState.offsetY,
    display.workArea,
    width,
    height,
  );
  windowRef.setPosition(position.x, position.y);
});
ipcMain.on("orb-drag-end", () => {
  writeCurrentWindowState();
  dragState = null;
});

ipcMain.on("orb-resize-start", (_event, payload) => {
  if (!windowRef || windowRef.isDestroyed()) {
    return;
  }
  const point = pointFromPayload(payload);
  if (!point) {
    return;
  }
  const [width, height] = windowRef.getSize();
  resizeState = {
    startX: point.x,
    startY: point.y,
    startWidth: width,
    startHeight: height,
  };
  moveMode = true;
  windowRef.setIgnoreMouseEvents(false);
  windowRef.webContents.send("move-mode", true);
  log("resize mode: on");
});

ipcMain.on("orb-resize", (_event, payload) => {
  if (!resizeState || !windowRef || windowRef.isDestroyed()) {
    return;
  }
  const point = pointFromPayload(payload);
  if (!point) {
    return;
  }
  const width = Math.max(MIN_SIZE, resizeState.startWidth + point.x - resizeState.startX);
  const height = Math.max(MIN_SIZE, resizeState.startHeight + point.y - resizeState.startY);
  windowRef.setSize(Math.round(width), Math.round(height));
  syncRendererScale();
  scheduleWindowStateWrite();
});

ipcMain.on("orb-resize-end", () => {
  if (!resizeState) {
    return;
  }
  writeCurrentWindowState();
  resizeState = null;
  setMoveMode(false);
  log("resize mode: off");
});

app.whenReady().then(() => {
  app.setAppUserModelId("Codex.StrandOrb");
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(permission === "media" && inputEnabled());
  });
  writePid();
  startAudioSocket();
  createWindow();
});

app.on("before-quit", () => {
  writeCurrentWindowState();
  setMoveMode(false);
  stopAvatarStateWatcher();
  rendererReady = false;
  if (socket) {
    socket.close();
    socket = null;
  }
  removePid();
});

app.on("window-all-closed", (event) => {
  event.preventDefault();
});

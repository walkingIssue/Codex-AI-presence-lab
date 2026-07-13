const { app, BrowserWindow, ipcMain, screen, session } = require("electron");
const dgram = require("node:dgram");
const fs = require("node:fs");
const path = require("node:path");
const { framePolicyArgument, framePolicyFromEnvironment } = require("./frame_policy.cjs");
const { avatarStateForWindow, routeWindowKeys, windowDescriptors } = require("./presence_windows.cjs");
const { createVoiceControlRunner } = require("./voice_control.cjs");

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
const PRESENCE_PROFILES_PATH = path.join(VOICE_ROOT, "presence-profiles.json");
const AVATAR_STATE_PATH = path.join(VOICE_ROOT, "avatar-state.json");
const AVATAR_STATES_PATH = path.join(VOICE_ROOT, "avatar-states.json");
const AVATAR_STATUS_PATH = path.join(VOICE_ROOT, "avatar-state-status.json");
const AVATAR_STATUSES_PATH = path.join(VOICE_ROOT, "avatar-state-statuses.json");
const INPUT_SETTINGS_PATH = path.join(VOICE_ROOT, "input.json");
const INPUT_RECORDINGS_ROOT = path.join(VOICE_ROOT, "inbox", "recordings");
const AVATAR_STATE_SCHEMA = "codex-ai-presence/avatar-state/v0.1";
const ROUTE_AVATAR_STATE_SCHEMA = "codex-ai-presence/avatar-state/v0.2";
const AVATAR_STATE_LEDGER_SCHEMA = "codex-ai-presence/avatar-state-ledger/v0.1";
const AVATAR_STATUS_LEDGER_SCHEMA = "codex-ai-presence/avatar-state-status-ledger/v0.1";
const AVATAR_STATE_CAPABILITY = "avatar-state-v1";
const ACTION_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const SOURCE_PATTERN = /^[A-Za-z0-9._:-]{1,80}$/;
const MAX_ACTIONS = 128;
const FRAME_POLICY = framePolicyFromEnvironment();
const FRAME_POLICY_ARGUMENT = framePolicyArgument(FRAME_POLICY);
let windowRef = null;
let socket = null;
let moveMode = false;
let rendererReady = false;
let avatarStateWatcher = false;
let activeAvatar = null;
let activeDescriptor = null;
let presenceDescriptors = [];
let foregroundWindowKey = null;
const secondaryWindows = new Map();
let acceptedProjectAvatarState = null;
const acceptedRouteAvatarStates = new Map();
const lastAvatarRevisions = new Map();
const windowStateWriteTimers = new Map();
const dragStates = new Map();
const resizeStates = new Map();

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

const runVoiceInput = createVoiceControlRunner({
  python: process.platform === "win32"
    ? path.join(VOICE_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(VOICE_ROOT, ".venv", "bin", "python"),
  script: path.join(VOICE_ROOT, "voice_input.py"),
  voiceRoot: VOICE_ROOT,
  projectRoot: PROJECT_ROOT,
});

function recordingPath(recordingId) {
  if (typeof recordingId !== "string" || !/^[a-zA-Z0-9_-]{1,80}$/.test(recordingId)) {
    throw new Error("invalid recording id");
  }
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

function selectedAvatarId() {
  try {
    const selection = JSON.parse(fs.readFileSync(AVATAR_SELECTION_PATH, "utf8"));
    const avatarId = typeof selection.avatar_id === "string" ? selection.avatar_id : "";
    return /^[a-z0-9][a-z0-9-]{0,63}$/.test(avatarId) ? avatarId : "builtin";
  } catch (_) {
    return "builtin";
  }
}

function configuredPresenceDescriptors() {
  let document = null;
  try {
    document = JSON.parse(fs.readFileSync(PRESENCE_PROFILES_PATH, "utf8"));
  } catch (_) {
    // A missing or invalid profile document preserves the legacy single avatar.
  }
  return windowDescriptors(document, selectedAvatarId());
}

function selectedAvatarInfo(requestedAvatarId = null) {
  try {
    const avatarId = typeof requestedAvatarId === "string" ? requestedAvatarId : selectedAvatarId();
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
    avatar_id: state?.avatar_id || activeAvatar?.id || "builtin",
    accepted: Boolean(accepted),
    reason,
    revision: Number.isInteger(state?.revision) ? state.revision : null,
    action_count: Array.isArray(state?.actions) ? state.actions.length : 0,
    scope: state?.scope || null,
    session_id: state?.session_id || null,
    profile_id: state?.profile_id || null,
    route_key: state?.route_key || null,
    updated_at: new Date().toISOString(),
  };
  try {
    fs.writeFileSync(AVATAR_STATUS_PATH, `${JSON.stringify(status, null, 2)}\n`, "utf8");
    if (status.route_key) {
      let ledger = null;
      try {
        ledger = JSON.parse(fs.readFileSync(AVATAR_STATUSES_PATH, "utf8"));
      } catch (_) {
        // The first routed status creates its owned diagnostics ledger.
      }
      if (
        !ledger
        || ledger.schema !== AVATAR_STATUS_LEDGER_SCHEMA
        || ledger.type !== "avatar-state-status-ledger"
        || !ledger.statuses
        || typeof ledger.statuses !== "object"
        || Array.isArray(ledger.statuses)
      ) {
        ledger = {
          schema: AVATAR_STATUS_LEDGER_SCHEMA,
          type: "avatar-state-status-ledger",
          statuses: {},
        };
      }
      ledger.statuses[status.route_key] = status;
      ledger.updated_at = status.updated_at;
      const temporary = `${AVATAR_STATUSES_PATH}.tmp`;
      fs.writeFileSync(temporary, `${JSON.stringify(ledger, null, 2)}\n`, "utf8");
      fs.copyFileSync(temporary, AVATAR_STATUSES_PATH);
      fs.unlinkSync(temporary);
    }
  } catch (error) {
    log(`avatar status write failed: ${error.message}`);
  }
}

function parseAvatarState(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("avatar state must be a JSON object");
  }
  if (![AVATAR_STATE_SCHEMA, ROUTE_AVATAR_STATE_SCHEMA].includes(value.schema) || value.type !== "avatar-state") {
    throw new Error("unsupported avatar state schema");
  }
  if (typeof value.avatar_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.avatar_id)) {
    throw new Error("avatar state has an invalid avatar_id");
  }
  if (typeof value.source !== "string" || !SOURCE_PATTERN.test(value.source)) {
    throw new Error("avatar state has an invalid source");
  }
  if (value.scope === "project") {
    if (value.schema !== AVATAR_STATE_SCHEMA) {
      throw new Error("project avatar state must use the v0.1 schema");
    }
  } else if (value.scope === "route") {
    if (value.schema !== ROUTE_AVATAR_STATE_SCHEMA) {
      throw new Error("routed avatar state must use the v0.2 schema");
    }
    if (typeof value.session_id !== "string" || !value.session_id) {
      throw new Error("routed avatar state has an invalid session_id");
    }
    if (typeof value.profile_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.profile_id)) {
      throw new Error("routed avatar state has an invalid profile_id");
    }
    const expectedRoute = `session:${value.session_id}|profile:${value.profile_id}`;
    if (value.route_key !== expectedRoute) {
      throw new Error("routed avatar state has an invalid route_key");
    }
  } else {
    throw new Error("avatar state scope must be project or route");
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

function rendererWindows() {
  const renderers = [];
  if (windowRef && !windowRef.isDestroyed() && activeDescriptor && activeAvatar) {
    renderers.push({
      key: activeDescriptor.key,
      descriptor: activeDescriptor,
      avatar: activeAvatar,
      window: windowRef,
      ready: rendererReady,
      moveMode,
      primary: true,
    });
  }
  renderers.push(...secondaryWindows.values());
  return renderers;
}

function rendererForKey(key) {
  return rendererWindows().find((renderer) => renderer.key === key) || null;
}

function avatarStateForRenderer(renderer) {
  return avatarStateForWindow(
    renderer.descriptor,
    renderer.avatar.id,
    acceptedRouteAvatarStates,
    acceptedProjectAvatarState,
  );
}

function sendAvatarState(targetRenderer = null) {
  const renderers = targetRenderer ? [targetRenderer] : rendererWindows();
  for (const renderer of renderers) {
    const state = avatarStateForRenderer(renderer);
    if (state && renderer.ready && !renderer.window.isDestroyed()) {
      renderer.window.webContents.send("avatar-state", state);
    }
  }
}

function acceptAvatarState(raw) {
  let state;
  try {
    state = parseAvatarState(raw);
  } catch (error) {
    writeAvatarStatus("invalid-state", null, false);
    log(`avatar state rejected: ${error.message}`);
    return;
  }
  const stateRenderers = rendererWindows().filter((renderer) => (
    renderer.avatar.id === state.avatar_id
    && (state.scope !== "route" || renderer.key === state.route_key)
  ));
  if (!stateRenderers.length) {
    writeAvatarStatus("avatar-mismatch", state, false);
    return;
  }
  if (!stateRenderers.some((renderer) => renderer.avatar.id !== "builtin" && renderer.avatar.stateSupported)) {
    writeAvatarStatus("unsupported-capability", state, false);
    return;
  }
  const revisionKey = `${state.scope}:${state.route_key || "project"}:${state.avatar_id}:${state.source}`;
  const lastRevision = lastAvatarRevisions.get(revisionKey);
  if (lastRevision !== undefined && state.revision <= lastRevision) {
    const acceptedState = state.scope === "route"
      ? acceptedRouteAvatarStates.get(state.route_key)
      : acceptedProjectAvatarState;
    if (state.revision === lastRevision && acceptedState?.source === state.source) {
      writeAvatarStatus("accepted", acceptedState, true);
      return;
    }
    writeAvatarStatus("stale-revision", state, false);
    return;
  }
  lastAvatarRevisions.set(revisionKey, state.revision);
  if (state.scope === "route") acceptedRouteAvatarStates.set(state.route_key, state);
  else acceptedProjectAvatarState = state;
  writeAvatarStatus("accepted", state, true);
  if (state.scope === "route") stateRenderers.forEach((renderer) => sendAvatarState(renderer));
  else sendAvatarState();
}

function loadAvatarState() {
  try {
    if (fs.statSync(AVATAR_STATE_PATH).size > 64 * 1024) {
      throw new Error("avatar state exceeds the 64 KiB limit");
    }
    acceptAvatarState(JSON.parse(fs.readFileSync(AVATAR_STATE_PATH, "utf8")));
  } catch (error) {
    if (error.code === "ENOENT") return;
    writeAvatarStatus("invalid-json", null, false);
    log(`avatar state read failed: ${error.message}`);
  }
}

function loadRoutedAvatarStates() {
  try {
    if (fs.statSync(AVATAR_STATES_PATH).size > 512 * 1024) {
      throw new Error("routed avatar state exceeds the 512 KiB limit");
    }
    const ledger = JSON.parse(fs.readFileSync(AVATAR_STATES_PATH, "utf8"));
    if (
      ledger?.schema !== AVATAR_STATE_LEDGER_SCHEMA
      || ledger?.type !== "avatar-state-ledger"
      || !ledger.states
      || typeof ledger.states !== "object"
      || Array.isArray(ledger.states)
    ) {
      throw new Error("routed avatar-state ledger is invalid");
    }
    const states = Object.entries(ledger.states)
      .map(([routeKey, value]) => ({ routeKey, value }))
      .sort((left, right) => String(left.value?.issued_at || "").localeCompare(String(right.value?.issued_at || "")));
    for (const { routeKey, value } of states) {
      if (value?.route_key !== routeKey) {
        throw new Error("routed avatar-state ledger key mismatch");
      }
      acceptAvatarState(value);
    }
  } catch (error) {
    if (error.code === "ENOENT") return;
    writeAvatarStatus("invalid-route-ledger", null, false);
    log(`routed avatar state read failed: ${error.message}`);
  }
}

function startAvatarStateWatcher() {
  if (avatarStateWatcher) {
    return;
  }
  avatarStateWatcher = true;
  fs.watchFile(AVATAR_STATE_PATH, { persistent: false, interval: 200 }, loadAvatarState);
  fs.watchFile(AVATAR_STATES_PATH, { persistent: false, interval: 200 }, loadRoutedAvatarStates);
  loadAvatarState();
  loadRoutedAvatarStates();
}

function stopAvatarStateWatcher() {
  if (!avatarStateWatcher) {
    return;
  }
  fs.unwatchFile(AVATAR_STATE_PATH, loadAvatarState);
  fs.unwatchFile(AVATAR_STATES_PATH, loadRoutedAvatarStates);
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

function normalizedWindowState(value) {
  if (!value || typeof value !== "object" || !Number.isFinite(value.x) || !Number.isFinite(value.y)) {
    return null;
  }
  return {
    x: Math.round(value.x),
    y: Math.round(value.y),
    width: normalizeWindowDimension(value.width),
    height: normalizeWindowDimension(value.height),
  };
}

function readSavedWindowStateDocument() {
  try {
    const value = JSON.parse(fs.readFileSync(POSITION_PATH, "utf8"));
    if (value?.version === 3 && value.windows && typeof value.windows === "object" && !Array.isArray(value.windows)) {
      return value;
    }
    const legacy = normalizedWindowState(value);
    return legacy ? { version: 2, legacy } : { version: 3, windows: {} };
  } catch (_) {
    // Missing or malformed position state falls back to the default location.
  }
  return { version: 3, windows: {} };
}

function savedWindowState(routeKey, { allowLegacy = false } = {}) {
  const document = readSavedWindowStateDocument();
  const routed = document.version === 3 ? normalizedWindowState(document.windows?.[routeKey]) : null;
  return routed || (allowLegacy ? document.legacy || null : null);
}

function writeRendererWindowState(renderer) {
  if (!renderer || renderer.window.isDestroyed()) return;
  try {
    const document = readSavedWindowStateDocument();
    const windows = document.version === 3 && document.windows && typeof document.windows === "object"
      ? { ...document.windows }
      : {};
    const [x, y] = renderer.window.getPosition();
    const [width, height] = renderer.window.getSize();
    windows[renderer.key] = {
      x,
      y,
      width: normalizeWindowDimension(width),
      height: normalizeWindowDimension(height),
      updatedAt: new Date().toISOString(),
    };
    const temporary = `${POSITION_PATH}.tmp`;
    fs.writeFileSync(
      temporary,
      `${JSON.stringify(
        {
          version: 3,
          windows,
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

function scheduleRendererWindowStateWrite(renderer) {
  const existing = windowStateWriteTimers.get(renderer.key);
  if (existing !== undefined) {
    clearTimeout(existing);
  }
  const timer = setTimeout(() => {
    windowStateWriteTimers.delete(renderer.key);
    writeRendererWindowState(renderer);
  }, 120);
  windowStateWriteTimers.set(renderer.key, timer);
}

function startupPosition(primaryDisplay, descriptor, index = 0, primaryState = null) {
  const saved = savedWindowState(descriptor.key, { allowLegacy: index === 0 });
  const width = saved?.width || SIZE;
  const height = saved?.height || SIZE;
  if (saved) {
    const display = screen.getDisplayNearestPoint(saved);
    const position = clampPosition(saved.x, saved.y, display.workArea, width, height);
    return { ...position, width, height };
  }
  const workArea = primaryDisplay.workArea;
  if (primaryState && index > 0) {
    const position = clampPosition(
      primaryState.x - index * (primaryState.width + 16),
      primaryState.y,
      workArea,
      primaryState.width,
      primaryState.height,
    );
    return { ...position, width: primaryState.width, height: primaryState.height };
  }
  const position = clampPosition(
    workArea.x + workArea.width - width - DEFAULT_MARGIN,
    workArea.y + workArea.height - height - DEFAULT_MARGIN,
    workArea,
    width,
    height,
  );
  return { ...position, width, height };
}

function pointFromPayload(payload) {
  const x = Number(payload && payload.screenX);
  const y = Number(payload && payload.screenY);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

function routeAudioEvent(event) {
  if (event?.type === "voice-output") {
    const owner = presenceDescriptors.find((descriptor) => (
      (typeof event.route_key === "string" && descriptor.key === event.route_key)
      || (
        typeof event.session_id === "string"
        && descriptor.sessionId === event.session_id
        && (!event.profile_id || descriptor.profileId === event.profile_id)
      )
    ));
    // Clearing an unknown owner prevents its following unscoped Kokoro frames
    // from animating whichever session happened to speak previously.
    foregroundWindowKey = owner?.key || null;
  }
  const targetKeys = new Set(routeWindowKeys(presenceDescriptors, event, foregroundWindowKey));
  for (const renderer of rendererWindows()) {
    if (renderer.ready && targetKeys.has(renderer.key) && !renderer.window.isDestroyed()) {
      renderer.window.webContents.send("audio-event", event);
    }
  }
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
      routeAudioEvent(event);
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

function syncSecondaryRendererScale(renderer) {
  if (!renderer.ready || renderer.window.isDestroyed()) return;
  const [width, height] = renderer.window.getContentSize();
  const baseDimension = Math.min(width, height);
  const rendererKeepsNativeScale = renderer.avatar.id === "builtin" || renderer.avatar.stateSupported;
  const scale = rendererKeepsNativeScale ? 1 : Math.max(0.25, baseDimension / SIZE);
  try {
    renderer.window.webContents.setZoomFactor(scale);
    renderer.window.webContents.send("window-resize", { width, height });
  } catch (error) {
    log(`secondary renderer scale update failed key=${renderer.key}: ${error.message}`);
  }
}

function createSecondaryWindow(descriptor, index, primaryState) {
  const avatar = selectedAvatarInfo(descriptor.avatarId);
  const display = screen.getDisplayNearestPoint(primaryState);
  const windowState = startupPosition(display, descriptor, index, primaryState);
  const satellite = new BrowserWindow({
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
      additionalArguments: [FRAME_POLICY_ARGUMENT],
    },
  });
  const renderer = {
    key: descriptor.key,
    descriptor,
    avatar,
    window: satellite,
    ready: false,
    moveMode: false,
    primary: false,
  };
  secondaryWindows.set(descriptor.key, renderer);
  satellite.setAlwaysOnTop(true, "floating");
  satellite.setIgnoreMouseEvents(true, { forward: true });
  satellite.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    log(`renderer console key=${descriptor.key} level=${level} ${sourceId}:${line} ${message}`);
  });
  satellite.webContents.on("context-menu", (event) => event.preventDefault());
  satellite.webContents.on("render-process-gone", (_event, details) => {
    log(`renderer gone key=${descriptor.key} reason=${details.reason} exitCode=${details.exitCode}`);
  });
  satellite.webContents.on("did-fail-load", (_event, code, description) => {
    log(`load failed key=${descriptor.key} code=${code} ${description}`);
  });
  log(`renderer entry key=${descriptor.key}: ${avatar.entry}`);
  satellite.loadFile(avatar.entry);
  satellite.webContents.once("did-finish-load", () => {
    renderer.ready = true;
    syncSecondaryRendererScale(renderer);
    satellite.webContents.send("move-mode", false);
    sendAvatarState(renderer);
    satellite.showInactive();
    log(`renderer loaded key=${descriptor.key}`);
  });
  satellite.on("resize", () => {
    syncSecondaryRendererScale(renderer);
    scheduleRendererWindowStateWrite(renderer);
  });
  satellite.on("move", () => scheduleRendererWindowStateWrite(renderer));
  satellite.on("closed", () => {
    const timer = windowStateWriteTimers.get(renderer.key);
    if (timer !== undefined) clearTimeout(timer);
    windowStateWriteTimers.delete(renderer.key);
    dragStates.delete(renderer.key);
    resizeStates.delete(renderer.key);
    renderer.ready = false;
    secondaryWindows.delete(descriptor.key);
  });
}

function createWindow() {
  const display = screen.getPrimaryDisplay();
  presenceDescriptors = configuredPresenceDescriptors();
  activeDescriptor = presenceDescriptors[0];
  const windowState = startupPosition(display, activeDescriptor);
  foregroundWindowKey = activeDescriptor.key;
  activeAvatar = selectedAvatarInfo(activeDescriptor.avatarId);
  log(
    `avatar selected key=${activeDescriptor.key}: ${activeAvatar.id}; `
    + `state bridge: ${activeAvatar.stateSupported ? "supported" : "disabled"}; `
    + `presence windows: ${presenceDescriptors.length}`,
  );
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
      additionalArguments: [FRAME_POLICY_ARGUMENT],
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
    sendAvatarState(rendererForKey(activeDescriptor.key));
    windowRef.showInactive();
  });
  windowRef.on("resize", () => {
    syncRendererScale();
    const renderer = rendererForKey(activeDescriptor.key);
    if (renderer) scheduleRendererWindowStateWrite(renderer);
  });
  windowRef.on("move", () => {
    const renderer = rendererForKey(activeDescriptor.key);
    if (renderer) scheduleRendererWindowStateWrite(renderer);
  });
  windowRef.on("closed", () => {
    const key = activeDescriptor?.key;
    const timer = key ? windowStateWriteTimers.get(key) : undefined;
    if (timer !== undefined) clearTimeout(timer);
    if (key) {
      windowStateWriteTimers.delete(key);
      dragStates.delete(key);
      resizeStates.delete(key);
    }
    rendererReady = false;
    windowRef = null;
  });
  presenceDescriptors.slice(1).forEach((descriptor, index) => {
    createSecondaryWindow(descriptor, index + 1, windowState);
  });
  startAvatarStateWatcher();
}

function isPrimarySender(event) {
  return Boolean(windowRef && !windowRef.isDestroyed() && event.sender.id === windowRef.webContents.id);
}

function rendererForSender(event) {
  if (isPrimarySender(event)) {
    return {
      key: activeDescriptor.key,
      descriptor: activeDescriptor,
      avatar: activeAvatar,
      window: windowRef,
      ready: rendererReady,
      moveMode,
      primary: true,
    };
  }
  for (const renderer of secondaryWindows.values()) {
    if (!renderer.window.isDestroyed() && event.sender.id === renderer.window.webContents.id) {
      return renderer;
    }
  }
  return null;
}

function setRendererMoveMode(renderer, enabled) {
  const next = Boolean(enabled);
  if (renderer.primary) moveMode = next;
  else renderer.moveMode = next;
  dragStates.delete(renderer.key);
  renderer.window.setIgnoreMouseEvents(!next, { forward: true });
  renderer.window.webContents.send("move-mode", next);
  log(`move mode key=${renderer.key}: ${next ? "on" : "off"}`);
}

ipcMain.on("close-orb", () => app.quit());
ipcMain.on("set-move-mode", (event, enabled) => {
  const renderer = rendererForSender(event);
  if (renderer) setRendererMoveMode(renderer, enabled);
});
ipcMain.handle("voice-input-config", () => inputSettings());
ipcMain.handle("voice-record-start", async (event) => {
  const renderer = rendererForSender(event);
  if (!renderer) return { ok: false, error: "avatar_window_unknown" };
  if (!inputEnabled()) {
    return { ok: false, error: "voice_input_disabled" };
  }
  const controlArgs = ["control", "capture-start"];
  if (renderer.descriptor.sessionId) {
    controlArgs.push("--target-session-id", renderer.descriptor.sessionId);
  }
  const result = await runVoiceInput(controlArgs);
  if (!renderer.window.isDestroyed() && result.ok) {
    renderer.window.webContents.send("voice-input-state", {
      state: "listening",
      session_id: result.target_session_id,
      capture_sequence: result.capture_sequence,
    });
  } else if (!renderer.window.isDestroyed() && !result.ok) {
    renderer.window.webContents.send("voice-input-state", { state: "error", error: result.error || "voice_input_failed" });
  }
  return result;
});
ipcMain.handle("voice-record-finish", async (event, payload) => {
  if (!rendererForSender(event)) return { ok: false, error: "avatar_window_unknown" };
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
    await fs.promises.mkdir(INPUT_RECORDINGS_ROOT, { recursive: true });
    const destination = recordingPath(payload.recording_id);
    await fs.promises.writeFile(destination, Buffer.from(bytes));
    const result = await runVoiceInput([
      "control",
      "capture-finish",
      "--recording",
      destination,
      "--capture-sequence",
      String(captureSequence),
    ]);
    if (!result.ok) {
      await fs.promises.rm(destination, { force: true });
    }
    return result;
  } catch (error) {
    return { ok: false, error: error.message };
  }
});
ipcMain.handle("voice-record-cancel", async (event) => (
  rendererForSender(event)
    ? await runVoiceInput(["control", "capture-cancel"])
    : { ok: false, error: "avatar_window_unknown" }
));
ipcMain.on("orb-drag-start", (event, payload) => {
  const renderer = rendererForSender(event);
  if (!renderer || !renderer.moveMode || renderer.window.isDestroyed()) return;
  const point = pointFromPayload(payload);
  if (!point) return;
  const [x, y] = renderer.window.getPosition();
  dragStates.set(renderer.key, { offsetX: point.x - x, offsetY: point.y - y });
});
ipcMain.on("orb-drag", (event, payload) => {
  const renderer = rendererForSender(event);
  if (!renderer || !renderer.moveMode || renderer.window.isDestroyed()) return;
  const dragState = dragStates.get(renderer.key);
  if (!dragState) return;
  const point = pointFromPayload(payload);
  if (!point) return;
  const display = screen.getDisplayNearestPoint(point);
  const [width, height] = renderer.window.getSize();
  const position = clampPosition(
    point.x - dragState.offsetX,
    point.y - dragState.offsetY,
    display.workArea,
    width,
    height,
  );
  renderer.window.setPosition(position.x, position.y);
});
ipcMain.on("orb-drag-end", (event) => {
  const renderer = rendererForSender(event);
  if (!renderer) return;
  writeRendererWindowState(renderer);
  dragStates.delete(renderer.key);
});

ipcMain.on("orb-resize-start", (event, payload) => {
  const renderer = rendererForSender(event);
  if (!renderer || renderer.window.isDestroyed()) return;
  const point = pointFromPayload(payload);
  if (!point) return;
  const [width, height] = renderer.window.getSize();
  resizeStates.set(renderer.key, {
    startX: point.x,
    startY: point.y,
    startWidth: width,
    startHeight: height,
  });
  setRendererMoveMode(renderer, true);
  log(`resize mode key=${renderer.key}: on`);
});

ipcMain.on("orb-resize", (event, payload) => {
  const renderer = rendererForSender(event);
  if (!renderer || renderer.window.isDestroyed()) return;
  const resizeState = resizeStates.get(renderer.key);
  if (!resizeState) return;
  const point = pointFromPayload(payload);
  if (!point) return;
  const width = Math.max(MIN_SIZE, resizeState.startWidth + point.x - resizeState.startX);
  const height = Math.max(MIN_SIZE, resizeState.startHeight + point.y - resizeState.startY);
  renderer.window.setSize(Math.round(width), Math.round(height));
  if (renderer.primary) syncRendererScale();
  else syncSecondaryRendererScale(renderer);
  scheduleRendererWindowStateWrite(renderer);
});

ipcMain.on("orb-resize-end", (event) => {
  const renderer = rendererForSender(event);
  if (!renderer || !resizeStates.has(renderer.key)) return;
  writeRendererWindowState(renderer);
  resizeStates.delete(renderer.key);
  setRendererMoveMode(renderer, false);
  log(`resize mode key=${renderer.key}: off`);
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
  for (const renderer of rendererWindows()) {
    writeRendererWindowState(renderer);
    setRendererMoveMode(renderer, false);
  }
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

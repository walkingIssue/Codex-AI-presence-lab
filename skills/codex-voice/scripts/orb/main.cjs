const { app, BrowserWindow, ipcMain, screen } = require("electron");
const dgram = require("node:dgram");
const fs = require("node:fs");
const path = require("node:path");

const PORT = Number(process.env.CODEX_ORB_PORT || 17831);
const SIZE = 440;
const DEFAULT_MARGIN = 36;
const PID_PATH = path.join(__dirname, "orb.pid");
const LOG_PATH = path.join(__dirname, "orb.log");
const POSITION_PATH = path.join(__dirname, "..", "orb-position.json");
let windowRef = null;
let socket = null;
let moveMode = false;
let dragState = null;

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

function clampPosition(x, y, workArea) {
  const maxX = Math.max(workArea.x, workArea.x + workArea.width - SIZE);
  const maxY = Math.max(workArea.y, workArea.y + workArea.height - SIZE);
  return {
    x: Math.max(workArea.x, Math.min(Math.round(x), maxX)),
    y: Math.max(workArea.y, Math.min(Math.round(y), maxY)),
  };
}

function readSavedPosition() {
  try {
    const value = JSON.parse(fs.readFileSync(POSITION_PATH, "utf8"));
    if (Number.isFinite(value.x) && Number.isFinite(value.y)) {
      return { x: value.x, y: value.y };
    }
  } catch (_) {
    // Missing or malformed position state falls back to the default location.
  }
  return null;
}

function writePosition(x, y) {
  try {
    const temporary = `${POSITION_PATH}.tmp`;
    fs.writeFileSync(
      temporary,
      `${JSON.stringify({ version: 1, x, y, updatedAt: new Date().toISOString() }, null, 2)}\n`,
      "utf8",
    );
    fs.copyFileSync(temporary, POSITION_PATH);
    fs.unlinkSync(temporary);
  } catch (error) {
    log(`position write failed: ${error.message}`);
  }
}

function startupPosition(primaryDisplay) {
  const saved = readSavedPosition();
  if (saved) {
    const display = screen.getDisplayNearestPoint(saved);
    return clampPosition(saved.x, saved.y, display.workArea);
  }
  const workArea = primaryDisplay.workArea;
  return clampPosition(
    workArea.x + workArea.width - SIZE - DEFAULT_MARGIN,
    workArea.y + workArea.height - SIZE - DEFAULT_MARGIN,
    workArea,
  );
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

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const position = startupPosition(display);

  windowRef = new BrowserWindow({
    width: SIZE,
    height: SIZE,
    x: position.x,
    y: position.y,
    frame: false,
    transparent: true,
    resizable: false,
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
  windowRef.webContents.on("render-process-gone", (_event, details) => {
    log(`renderer gone reason=${details.reason} exitCode=${details.exitCode}`);
  });
  windowRef.webContents.on("did-fail-load", (_event, code, description) => {
    log(`load failed code=${code} ${description}`);
  });
  windowRef.loadFile(path.join(__dirname, "index.html"));
  windowRef.webContents.once("did-finish-load", () => {
    log("renderer loaded");
    windowRef.webContents.send("move-mode", moveMode);
    windowRef.showInactive();
  });
  windowRef.on("closed", () => {
    dragState = null;
    windowRef = null;
  });
}

ipcMain.on("close-orb", () => app.quit());
ipcMain.on("set-move-mode", (_event, enabled) => setMoveMode(enabled));
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
  const position = clampPosition(
    point.x - dragState.offsetX,
    point.y - dragState.offsetY,
    display.workArea,
  );
  windowRef.setPosition(position.x, position.y);
});
ipcMain.on("orb-drag-end", () => {
  if (windowRef && !windowRef.isDestroyed()) {
    const [x, y] = windowRef.getPosition();
    writePosition(x, y);
  }
  dragState = null;
});

app.whenReady().then(() => {
  app.setAppUserModelId("Codex.StrandOrb");
  writePid();
  startAudioSocket();
  createWindow();
});

app.on("before-quit", () => {
  setMoveMode(false);
  if (socket) {
    socket.close();
    socket = null;
  }
  removePid();
});

app.on("window-all-closed", (event) => {
  event.preventDefault();
});

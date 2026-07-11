const { app, BrowserWindow, ipcMain, screen } = require("electron");
const dgram = require("node:dgram");
const fs = require("node:fs");
const path = require("node:path");

const PORT = Number(process.env.CODEX_ORB_PORT || 17831);
const PID_PATH = path.join(__dirname, "orb.pid");
const LOG_PATH = path.join(__dirname, "orb.log");
let windowRef = null;
let socket = null;

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
  const size = 440;
  const workArea = display.workArea;

  windowRef = new BrowserWindow({
    width: size,
    height: size,
    x: Math.max(workArea.x, workArea.x + workArea.width - size - 36),
    y: Math.max(workArea.y, workArea.y + workArea.height - size - 36),
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
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
    windowRef.showInactive();
  });
  windowRef.on("closed", () => {
    windowRef = null;
  });
}

ipcMain.on("close-orb", () => app.quit());

app.whenReady().then(() => {
  app.setAppUserModelId("Codex.StrandOrb");
  writePid();
  startAudioSocket();
  createWindow();
});

app.on("before-quit", () => {
  if (socket) {
    socket.close();
    socket = null;
  }
  removePid();
});

app.on("window-all-closed", (event) => {
  event.preventDefault();
});

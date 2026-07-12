const { contextBridge, ipcRenderer } = require("electron");

const RESIZE_HANDLE_PX = 48;
let resizing = false;
let resizePointerId = null;
let requestedResizeMode = false;

function modifierHeld(event) {
  return Boolean(event.altKey && (event.ctrlKey || event.metaKey));
}

function inResizeHandle(event) {
  return event.clientX >= window.innerWidth - RESIZE_HANDLE_PX
    && event.clientY >= window.innerHeight - RESIZE_HANDLE_PX;
}

function resizePayload(event) {
  return { screenX: event.screenX, screenY: event.screenY };
}

function finishResize(event) {
  if (!resizing || (event && resizePointerId !== null && event.pointerId !== resizePointerId)) {
    return;
  }
  resizing = false;
  resizePointerId = null;
  ipcRenderer.send("orb-resize-end");
  if (event?.target?.releasePointerCapture && event.pointerId !== undefined) {
    try {
      event.target.releasePointerCapture(event.pointerId);
    } catch (_) {
      // The pointer may already have left the document.
    }
  }
}

// The Orb is click-through by default. This small, host-owned gesture gives
// every renderer—including custom avatars—a consistent resize affordance.
window.addEventListener("mousemove", (event) => {
  const wantsResize = modifierHeld(event) && event.shiftKey && inResizeHandle(event);
  if (wantsResize && !requestedResizeMode && !resizing) {
    requestedResizeMode = true;
    ipcRenderer.send("set-move-mode", true);
  } else if (!wantsResize && requestedResizeMode && !resizing) {
    requestedResizeMode = false;
    ipcRenderer.send("set-move-mode", false);
  }
  document.documentElement.style.cursor = wantsResize ? "nwse-resize" : "";
}, true);

window.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || !event.shiftKey || !modifierHeld(event) || !inResizeHandle(event)) {
    return;
  }
  resizing = true;
  resizePointerId = event.pointerId;
  requestedResizeMode = false;
  ipcRenderer.send("orb-resize-start", resizePayload(event));
  if (event.target?.setPointerCapture) {
    try {
      event.target.setPointerCapture(event.pointerId);
    } catch (_) {
      // Pointer capture is best-effort for transparent windows.
    }
  }
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

window.addEventListener("pointermove", (event) => {
  if (!resizing || (resizePointerId !== null && event.pointerId !== resizePointerId)) {
    return;
  }
  ipcRenderer.send("orb-resize", resizePayload(event));
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

window.addEventListener("pointerup", finishResize, true);
window.addEventListener("pointercancel", finishResize, true);
window.addEventListener("blur", () => finishResize(), true);

contextBridge.exposeInMainWorld("orbApi", {
  onAudioEvent(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("audio-event", listener);
    return () => ipcRenderer.removeListener("audio-event", listener);
  },
  onAvatarState(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("avatar-state", listener);
    return () => ipcRenderer.removeListener("avatar-state", listener);
  },
  onMoveMode(callback) {
    const listener = (_event, enabled) => callback(Boolean(enabled));
    ipcRenderer.on("move-mode", listener);
    return () => ipcRenderer.removeListener("move-mode", listener);
  },
  onWindowResize(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("window-resize", listener);
    return () => ipcRenderer.removeListener("window-resize", listener);
  },
  setMoveMode(enabled) {
    ipcRenderer.send("set-move-mode", Boolean(enabled));
  },
  dragStart(payload) {
    ipcRenderer.send("orb-drag-start", payload);
  },
  drag(payload) {
    ipcRenderer.send("orb-drag", payload);
  },
  dragEnd() {
    ipcRenderer.send("orb-drag-end");
  },
  close() {
    ipcRenderer.send("close-orb");
  },
});

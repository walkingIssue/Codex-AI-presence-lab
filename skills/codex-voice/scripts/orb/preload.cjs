const { contextBridge, ipcRenderer } = require("electron");

// Sandboxed Electron preloads cannot import local modules. These two functions
// are deliberately self-contained because executeInMainWorld serializes them.
function installFrameScheduler(idleFps, activeFps) {
  if (window.__codexPresenceFrameScheduler) return window.__codexPresenceFrameScheduler.policy();

  const nativeRequestAnimationFrame = window.requestAnimationFrame.bind(window);
  const nativeCancelAnimationFrame = window.cancelAnimationFrame.bind(window);
  const callbackLastRun = new WeakMap();
  const pending = new Map();
  let nextRequestId = 1;
  let mode = "idle";
  let idle = Math.max(1, Math.min(60, Number(idleFps) || 60));
  let active = Math.max(idle, Math.min(60, Number(activeFps) || 60));

  function requestAnimationFrameAtBudget(callback) {
    if (typeof callback !== "function") {
      throw new TypeError("requestAnimationFrame callback must be a function");
    }
    const requestId = nextRequestId;
    nextRequestId = nextRequestId >= Number.MAX_SAFE_INTEGER ? 1 : nextRequestId + 1;
    const request = { nativeId: 0 };
    pending.set(requestId, request);

    function pump(timestamp) {
      if (!pending.has(requestId)) return;
      const fps = mode === "active" ? active : idle;
      const minimumInterval = 1000 / fps;
      const lastRun = callbackLastRun.get(callback);
      if (lastRun === undefined || timestamp - lastRun >= minimumInterval - 0.5) {
        pending.delete(requestId);
        callbackLastRun.set(callback, timestamp);
        callback(timestamp);
        return;
      }
      request.nativeId = nativeRequestAnimationFrame(pump);
    }

    request.nativeId = nativeRequestAnimationFrame(pump);
    return requestId;
  }

  function cancelAnimationFrameAtBudget(requestId) {
    const request = pending.get(requestId);
    if (!request) return;
    pending.delete(requestId);
    nativeCancelAnimationFrame(request.nativeId);
  }

  const scheduler = Object.freeze({
    setMode(nextMode) {
      mode = nextMode === "active" ? "active" : "idle";
      return mode;
    },
    policy() {
      return Object.freeze({ mode, idleFps: idle, activeFps: active });
    },
  });

  Object.defineProperty(window, "requestAnimationFrame", {
    configurable: true,
    writable: true,
    value: requestAnimationFrameAtBudget,
  });
  Object.defineProperty(window, "cancelAnimationFrame", {
    configurable: true,
    writable: true,
    value: cancelAnimationFrameAtBudget,
  });
  Object.defineProperty(window, "__codexPresenceFrameScheduler", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: scheduler,
  });
  return scheduler.policy();
}

function setFrameSchedulerMode(mode) {
  return window.__codexPresenceFrameScheduler?.setMode(mode) ?? "unavailable";
}

function framePolicyFromArguments(args = []) {
  const prefix = "--codex-frame-policy=";
  const encoded = args.find((value) => typeof value === "string" && value.startsWith(prefix));
  if (!encoded) return { enabled: true, idleFps: 60, activeFps: 60 };
  try {
    const value = JSON.parse(decodeURIComponent(encoded.slice(prefix.length)));
    return value && typeof value === "object"
      ? value
      : { enabled: true, idleFps: 60, activeFps: 60 };
  } catch (_) {
    return { enabled: true, idleFps: 60, activeFps: 60 };
  }
}

function platformFromArguments(args = []) {
  const prefix = "--codex-platform=";
  const encoded = args.find((value) => typeof value === "string" && value.startsWith(prefix));
  return encoded ? encoded.slice(prefix.length).toLowerCase() : "unknown";
}

const framePolicy = framePolicyFromArguments(process.argv);
const runtimePlatform = platformFromArguments(process.argv);
const linuxWindowControls = runtimePlatform === "linux";

function markPlatformDocument() {
  const root = document.documentElement;
  if (root) root.dataset.codexPlatform = runtimePlatform;
}

markPlatformDocument();
if (!document.documentElement) {
  document.addEventListener("DOMContentLoaded", markPlatformDocument, { once: true });
}
let frameMode = "idle";
let frameIdleTimer = null;
if (framePolicy?.enabled !== false) {
  try {
    contextBridge.executeInMainWorld({
      func: installFrameScheduler,
      args: [framePolicy?.idleFps, framePolicy?.activeFps],
    });
  } catch (_) {
    // Frame budgeting is an optimization; renderer startup must remain fail-open.
  }
}

function applyFrameMode(nextMode, delayMilliseconds = 0) {
  if (framePolicy?.enabled === false) return;
  if (frameIdleTimer !== null) {
    clearTimeout(frameIdleTimer);
    frameIdleTimer = null;
  }
  const normalized = nextMode === "active" ? "active" : "idle";
  const apply = () => {
    frameIdleTimer = null;
    if (frameMode === normalized) return;
    frameMode = normalized;
    try {
      contextBridge.executeInMainWorld({ func: setFrameSchedulerMode, args: [normalized] });
    } catch (_) {
      // Custom renderers continue with their native cadence if the bridge is unavailable.
    }
  };
  if (delayMilliseconds > 0) frameIdleTimer = setTimeout(apply, delayMilliseconds);
  else apply();
}

const RESIZE_HANDLE_PX = 72;
let resizing = false;
let resizePointerId = null;
let requestedResizeMode = false;
let voiceInputEnabled = false;
let voiceMaxSeconds = 60;
let gesture = null;
let voiceRecorder = null;
let voiceStream = null;
let voiceRecordingId = null;
let voiceChunks = [];
let moveModeRequested = false;
let hostMoveMode = false;
let resizeMode = false;
let resizePreview = false;
let resizeHandleHovered = false;
let lastPointerActivityAt = 0;

function enabledFromPayload(payload) {
  return typeof payload === "object" && payload !== null
    ? Boolean(payload.enabled)
    : Boolean(payload);
}

function updateResizeVisualState() {
  const active = resizeMode || resizePreview;
  document.body?.classList.toggle("resize-mode", active);
  const root = document.documentElement;
  if (root) root.style.cursor = resizeHandleHovered || resizing ? "nwse-resize" : "";
}

function reportPointerActivity(event) {
  const screenX = Number(event?.screenX);
  const screenY = Number(event?.screenY);
  const now = performance.now();
  if (!Number.isFinite(screenX) || !Number.isFinite(screenY) || now - lastPointerActivityAt < 80) return;
  lastPointerActivityAt = now;
  ipcRenderer.send("orb-pointer-activity", {
    screenX,
    screenY,
    clientX: Number(event.clientX),
    clientY: Number(event.clientY),
  });
}

// Keep the shared gesture layer informed even when a custom renderer does not
// subscribe to the public move-mode callback itself.
ipcRenderer.on("move-mode", (_event, payload) => {
  hostMoveMode = enabledFromPayload(payload);
  moveModeRequested = hostMoveMode;
});
ipcRenderer.on("resize-mode", (_event, enabled) => {
  resizeMode = Boolean(enabled);
  if (!resizeMode && !resizing) resizePreview = false;
  updateResizeVisualState();
});

// Pointer activity supports non-Wayland targeting without changing the normal
// click-through behavior. Wayland ignores this signal and uses compositor
// focus from the desktop overview as its explicit renderer selection.
window.addEventListener("mousemove", reportPointerActivity, true);

function requestMoveMode(enabled) {
  const next = Boolean(enabled);
  if (moveModeRequested === next) return;
  moveModeRequested = next;
  ipcRenderer.send("set-move-mode", next);
}

ipcRenderer.invoke("voice-input-config").then((settings) => {
  voiceInputEnabled = settings?.input_enabled === true;
  voiceMaxSeconds = Math.max(1, Math.min(60, Number(settings?.max_record_seconds) || 60));
}).catch(() => {});

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

function voiceModifierHeld(event) {
  return Boolean(event.altKey && (event.ctrlKey || event.metaKey));
}

function gesturePayload(event) {
  return { screenX: event.screenX, screenY: event.screenY };
}

function chooseRecordingMimeType() {
  if (typeof MediaRecorder === "undefined") return "";
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  return candidates.find((value) => MediaRecorder.isTypeSupported(value)) || "";
}

function clearGestureTimer() {
  if (gesture?.timer) {
    clearTimeout(gesture.timer);
    gesture.timer = null;
  }
}

function releaseGenericMove() {
  requestMoveMode(false);
  if (gesture && gesture.pointerId != null && document.documentElement.hasPointerCapture?.(gesture.pointerId)) {
    try { document.documentElement.releasePointerCapture(gesture.pointerId); } catch (_) {}
  }
}

async function beginVoiceCapture(current) {
  if (!voiceInputEnabled || gesture !== current || current.mode !== "pending") return;
  current.mode = "record-requested";
  const response = await ipcRenderer.invoke("voice-record-start");
  if (!response?.ok) {
    if (gesture === current) gesture = null;
    releaseGenericMove();
    return;
  }
  current.captureSequence = Number(response.capture_sequence) || null;
  if (gesture !== current || current.releaseRequested) {
    await ipcRenderer.invoke("voice-record-cancel");
    if (gesture === current) gesture = null;
    releaseGenericMove();
    return;
  }
  try {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      throw new Error("microphone capture is unavailable in this Electron runtime");
    }
    voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (gesture !== current || current.releaseRequested) {
      voiceStream.getTracks().forEach((track) => track.stop());
      voiceStream = null;
      await ipcRenderer.invoke("voice-record-cancel");
      if (gesture === current) gesture = null;
      releaseGenericMove();
      return;
    }
    voiceChunks = [];
    voiceRecordingId = globalThis.crypto?.randomUUID?.() || `recording-${Date.now()}`;
    const mimeType = chooseRecordingMimeType();
    voiceRecorder = mimeType ? new MediaRecorder(voiceStream, { mimeType }) : new MediaRecorder(voiceStream);
    voiceRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) voiceChunks.push(event.data);
    };
    voiceRecorder.onstop = async () => {
      const recorder = voiceRecorder;
      voiceRecorder = null;
      voiceStream?.getTracks().forEach((track) => track.stop());
      voiceStream = null;
      const recordingId = voiceRecordingId;
      voiceRecordingId = null;
      const discard = Boolean(current.discard);
      if (discard || !recordingId) {
        voiceChunks = [];
        await ipcRenderer.invoke("voice-record-cancel");
      } else {
        const blob = new Blob(voiceChunks, { type: recorder?.mimeType || "audio/webm" });
        voiceChunks = [];
        const bytes = new Uint8Array(await blob.arrayBuffer());
        await ipcRenderer.invoke("voice-record-finish", {
          recording_id: recordingId,
          capture_sequence: current.captureSequence,
          bytes,
        });
      }
      if (gesture === current) gesture = null;
      releaseGenericMove();
    };
    current.mode = "recording";
    voiceRecorder.start(250);
    current.timer = setTimeout(() => {
      if (gesture === current && voiceRecorder && current.mode === "recording") {
        current.discard = false;
        voiceRecorder.stop();
      }
    }, voiceMaxSeconds * 1000);
  } catch (_) {
    voiceStream?.getTracks().forEach((track) => track.stop());
    voiceStream = null;
    voiceRecorder = null;
    await ipcRenderer.invoke("voice-record-cancel");
    if (gesture === current) gesture = null;
    releaseGenericMove();
  }
}

function stopVoiceCapture(discard) {
  if (!gesture) return;
  clearGestureTimer();
  gesture.releaseRequested = true;
  gesture.discard = Boolean(discard);
  if (voiceRecorder && voiceRecorder.state !== "inactive") {
    voiceRecorder.stop();
    return;
  }
  if (gesture.mode === "record-requested") {
    ipcRenderer.invoke("voice-record-cancel").catch(() => {});
  }
  gesture = null;
  releaseGenericMove();
}

function cancelGenericGesture() {
  if (!gesture) return;
  clearGestureTimer();
  if (gesture.mode === "drag") ipcRenderer.send("orb-drag-end");
  if (gesture.mode === "recording" || gesture.mode === "record-requested") stopVoiceCapture(true);
  else {
    gesture = null;
    releaseGenericMove();
  }
}

function finishResize(event) {
  if (!resizing || (event && resizePointerId !== null && event.pointerId !== resizePointerId)) {
    return;
  }
  resizing = false;
  resizePointerId = null;
  resizePreview = false;
  updateResizeVisualState();
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
  resizeHandleHovered = inResizeHandle(event);
  const modifierResize = linuxWindowControls && modifierHeld(event) && event.shiftKey;
  const fallbackResize = linuxWindowControls
    ? modifierResize && resizeHandleHovered
    : resizeHandleHovered;
  const wantsResize = resizeMode || fallbackResize;
  resizePreview = wantsResize;
  updateResizeVisualState();
  if (fallbackResize && !requestedResizeMode && !resizing) {
    requestedResizeMode = true;
    requestMoveMode(true);
  } else if (!fallbackResize && !resizeMode && requestedResizeMode && !resizing) {
    requestedResizeMode = false;
    requestMoveMode(false);
  }
}, true);

// Mouse movement is forwarded while the transparent window is click-through.
// Arm the host before pointerdown so both drag and right-button capture can
// arrive at the shared preload instead of being swallowed by the desktop.
window.addEventListener("mousemove", (event) => {
  if (resizing || gesture || resizeMode || event.shiftKey) return;
  requestMoveMode(modifierHeld(event));
}, true);

window.addEventListener("pointerdown", (event) => {
  const linuxResizeGesture = linuxWindowControls && event.shiftKey && modifierHeld(event);
  const resizeArmed = resizeMode || linuxResizeGesture || (!linuxWindowControls && resizeHandleHovered);
  if (event.button !== 0 || !resizeArmed || !inResizeHandle(event)) {
    return;
  }
  resizing = true;
  resizePointerId = event.pointerId;
  resizeHandleHovered = true;
  resizePreview = true;
  updateResizeVisualState();
  requestedResizeMode = false;
  ipcRenderer.send("orb-resize-start", resizePayload(event));
  if (event.target?.setPointerCapture) {
    try { document.documentElement.setPointerCapture(event.pointerId); } catch (_) {}
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

// Voice capture is handled in the shared preload so custom Electron avatar
// pages receive the same gesture as the built-in Orb. When input is enabled,
// the left Ctrl/Cmd+Alt gesture remains movement-only; right-button hold is
// reserved for recording.
window.addEventListener("mousemove", (event) => {
  if (gesture) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

window.addEventListener("pointerdown", (event) => {
  if (
    event.button !== 0
    || event.shiftKey
    || resizeMode
    || inResizeHandle(event)
    || (!voiceModifierHeld(event) && !hostMoveMode)
  ) {
    return;
  }
  clearGestureTimer();
  gesture = {
    pointerId: event.pointerId,
    button: 0,
    clientX: event.clientX,
    clientY: event.clientY,
    mode: hostMoveMode ? "drag" : "pending",
    releaseRequested: false,
    discard: false,
    timer: null,
  };
  requestMoveMode(true);
  if (gesture.mode === "drag") {
    ipcRenderer.send("orb-drag-start", gesturePayload(event));
  }
  if (event.target?.setPointerCapture) {
    try { document.documentElement.setPointerCapture(event.pointerId); } catch (_) {}
  }
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

// Right-button hold is the unambiguous voice-input gesture.  Unlike the
// legacy left-button gesture it starts immediately and never becomes a drag,
// so moving the pointer while speaking does not cancel or redirect capture.
window.addEventListener("pointerdown", (event) => {
  if (!voiceInputEnabled || gesture || event.button !== 2 || !voiceModifierHeld(event) || event.shiftKey) {
    return;
  }
  clearGestureTimer();
  gesture = {
    pointerId: event.pointerId,
    button: 2,
    clientX: event.clientX,
    clientY: event.clientY,
    mode: "pending",
    releaseRequested: false,
    discard: false,
    timer: null,
  };
  requestMoveMode(true);
  beginVoiceCapture(gesture);
  if (event.target?.setPointerCapture) {
    try { event.target.setPointerCapture(event.pointerId); } catch (_) {}
  }
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

window.addEventListener("pointermove", (event) => {
  if (!gesture || event.pointerId !== gesture.pointerId) return;
  if (gesture.button === 2) {
    event.preventDefault();
    event.stopImmediatePropagation();
    return;
  }
  if (gesture.mode === "pending") {
    const dx = event.clientX - gesture.clientX;
    const dy = event.clientY - gesture.clientY;
    if ((dx * dx) + (dy * dy) > 64) {
      clearGestureTimer();
      gesture.mode = "drag";
      ipcRenderer.send("orb-drag-start", gesturePayload(event));
      ipcRenderer.send("orb-drag", gesturePayload(event));
    }
  } else if (gesture.mode === "drag") {
    ipcRenderer.send("orb-drag", gesturePayload(event));
  }
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

window.addEventListener("pointerup", (event) => {
  if (!gesture || event.pointerId !== gesture.pointerId) return;
  if (gesture.mode === "drag") {
    ipcRenderer.send("orb-drag-end");
    gesture = null;
    releaseGenericMove();
  } else if (gesture.mode === "recording" || gesture.mode === "record-requested") {
    stopVoiceCapture(false);
  } else {
    clearGestureTimer();
    gesture = null;
    releaseGenericMove();
  }
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

window.addEventListener("pointercancel", () => cancelGenericGesture(), true);
window.addEventListener("blur", () => cancelGenericGesture(), true);
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !gesture) return;
  cancelGenericGesture();
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
window.addEventListener("keyup", (event) => {
  if (!voiceInputEnabled || !gesture || gesture.button !== 2) return;
  if ((event.key === "Control" || event.key === "Alt" || event.key === "Meta") && !voiceModifierHeld(event)) {
    stopVoiceCapture(false);
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

contextBridge.exposeInMainWorld("orbApi", {
  onAudioEvent(callback) {
    const listener = (_event, payload) => {
      if (payload?.type === "state") {
        applyFrameMode(payload.state === "speaking" ? "active" : "idle", payload.state === "speaking" ? 0 : 600);
      } else if (payload?.type === "activity") {
        const active = payload.state && payload.state !== "idle";
        applyFrameMode(active ? "active" : "idle", active ? 0 : 600);
      }
      callback(payload);
    };
    ipcRenderer.on("audio-event", listener);
    return () => ipcRenderer.removeListener("audio-event", listener);
  },
  onAvatarState(callback) {
    const listener = (_event, payload) => {
      applyFrameMode("active");
      callback(payload);
      applyFrameMode("idle", 1000);
    };
    ipcRenderer.on("avatar-state", listener);
    return () => ipcRenderer.removeListener("avatar-state", listener);
  },
  onProfileCuration(callback) {
    const listener = (_event, payload) => {
      applyFrameMode("active");
      callback(payload);
      applyFrameMode("idle", 1000);
    };
    ipcRenderer.on("profile-curation", listener);
    return () => ipcRenderer.removeListener("profile-curation", listener);
  },
  onMoveMode(callback) {
    const listener = (_event, payload) => {
      const enabled = enabledFromPayload(payload);
      hostMoveMode = enabled;
      moveModeRequested = enabled;
      applyFrameMode(enabled ? "active" : "idle", enabled ? 0 : 300);
      callback(enabled);
    };
    ipcRenderer.on("move-mode", listener);
    return () => ipcRenderer.removeListener("move-mode", listener);
  },
  onResizeMode(callback) {
    const listener = (_event, enabled) => {
      const active = Boolean(enabled);
      resizeMode = active;
      if (!active && !resizing) resizePreview = false;
      updateResizeVisualState();
      callback(active);
    };
    ipcRenderer.on("resize-mode", listener);
    return () => ipcRenderer.removeListener("resize-mode", listener);
  },
  onWindowResize(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("window-resize", listener);
    return () => ipcRenderer.removeListener("window-resize", listener);
  },
  onVoiceInputState(callback) {
    const listener = (_event, payload) => {
      const active = ["listening", "transcribing", "submitting"].includes(payload?.state);
      applyFrameMode(active ? "active" : "idle", active ? 0 : 600);
      callback(payload);
    };
    ipcRenderer.on("voice-input-state", listener);
    return () => ipcRenderer.removeListener("voice-input-state", listener);
  },
  voiceInputConfig() {
    return ipcRenderer.invoke("voice-input-config");
  },
  voiceRecordStart() {
    return ipcRenderer.invoke("voice-record-start");
  },
  voiceRecordFinish(payload) {
    return ipcRenderer.invoke("voice-record-finish", payload);
  },
  voiceRecordCancel() {
    return ipcRenderer.invoke("voice-record-cancel");
  },
  setMoveMode(enabled) {
    requestMoveMode(enabled);
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

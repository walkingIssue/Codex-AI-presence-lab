"use strict";

const { contextBridge, ipcRenderer } = require("electron");

let snapshotCallback = null;
let eventCallback = null;
let presentationCallback = null;
let presentationCancelCallback = null;
const INPUT_FEEDBACK_PHASES = new Set([
  "idle",
  "targeted",
  "move",
  "resize",
  "recording",
  "transcribing",
  "ready",
  "delivered",
  "failed",
]);
let runtimeFeedbackPhase = "idle";
let pointerFeedbackPhase = "idle";

function ensureInputFeedback() {
  let feedback = document.getElementById("presence-input-feedback");
  if (feedback) return feedback;
  feedback = document.createElement("div");
  feedback.id = "presence-input-feedback";
  feedback.setAttribute("aria-hidden", "true");
  (document.body || document.documentElement)?.appendChild(feedback);
  return feedback;
}

function renderInputFeedback() {
  if (!document.documentElement) return;
  const interactionActive = pointerFeedbackPhase === "move" || pointerFeedbackPhase === "resize";
  const phase = interactionActive
    ? pointerFeedbackPhase
    : runtimeFeedbackPhase !== "idle"
      ? runtimeFeedbackPhase
      : pointerFeedbackPhase;
  document.documentElement.dataset.presenceFeedback = phase;
}

function setRuntimeFeedback(phase) {
  runtimeFeedbackPhase = INPUT_FEEDBACK_PHASES.has(phase) ? phase : "idle";
  renderInputFeedback();
}

function setPointerFeedback(phase) {
  pointerFeedbackPhase = INPUT_FEEDBACK_PHASES.has(phase) ? phase : "idle";
  renderInputFeedback();
}

function updatePointerFeedback(event) {
  if (windowInteraction) {
    setPointerFeedback(windowInteraction.mode);
    return;
  }
  if (event?.ctrlKey && event?.altKey) {
    setPointerFeedback(event.shiftKey ? "resize" : "targeted");
    return;
  }
  setPointerFeedback("idle");
}

ipcRenderer.on("presence-snapshot", async (_event, snapshot) => {
  if (typeof snapshotCallback !== "function") {
    ipcRenderer.send("presence-snapshot-failed", {
      binding_id: snapshot?.binding_id,
      revision: snapshot?.revision,
      error: "renderer did not register a snapshot consumer",
    });
    return;
  }
  try {
    await snapshotCallback(snapshot);
    ipcRenderer.send("presence-snapshot-applied", {
      binding_id: snapshot.binding_id,
      revision: snapshot.revision,
    });
  } catch (error) {
    ipcRenderer.send("presence-snapshot-failed", {
      binding_id: snapshot?.binding_id,
      revision: snapshot?.revision,
      error: String(error?.message || error),
    });
  }
});

ipcRenderer.on("presence-event", async (_event, payload) => {
  if (typeof eventCallback !== "function") return;
  try {
    await eventCallback(payload);
  } catch (error) {
    ipcRenderer.send("presence-renderer-event-failed", {
      binding_id: payload?.binding_id,
      utterance_id: payload?.utterance_id,
      error: String(error?.message || error),
    });
  }
});

ipcRenderer.on("presence-presentation", async (_event, cue) => {
  if (typeof presentationCallback !== "function") {
    ipcRenderer.send("presence-presentation-failed", {
      binding_id: cue?.binding_id,
      sequence: cue?.sequence,
      error: "renderer did not register a presentation consumer",
    });
    return;
  }
  try {
    const result = await presentationCallback(cue);
    ipcRenderer.send("presence-presentation-completed", {
      binding_id: cue.binding_id,
      configuration_revision: cue.configuration_revision,
      sequence: cue.sequence,
      status: result?.status === "cancelled" ? "cancelled" : "completed",
    });
  } catch (error) {
    ipcRenderer.send("presence-presentation-failed", {
      binding_id: cue?.binding_id,
      sequence: cue?.sequence,
      error: String(error?.message || error),
    });
  }
});

ipcRenderer.on("presence-presentation-cancel", (_event, payload) => {
  if (typeof presentationCancelCallback === "function") {
    presentationCancelCallback(payload);
  }
});

contextBridge.exposeInMainWorld("presenceRenderer", Object.freeze({
  onSnapshot(callback) {
    if (typeof callback !== "function") throw new TypeError("snapshot callback is required");
    snapshotCallback = callback;
  },
  onEvent(callback) {
    if (typeof callback !== "function") throw new TypeError("event callback is required");
    eventCallback = callback;
  },
  onPresentation(callback) {
    if (typeof callback !== "function") throw new TypeError("presentation callback is required");
    presentationCallback = callback;
  },
  onPresentationCancel(callback) {
    if (typeof callback !== "function") throw new TypeError("presentation cancel callback is required");
    presentationCancelCallback = callback;
  },
  ready() {
    ipcRenderer.send("presence-renderer-ready");
  },
  failed(error) {
    ipcRenderer.send("presence-renderer-failed", {
      error: String(error || "renderer failed"),
    });
  },
}));

let voiceRecorder = null;
let voiceStream = null;
let voiceCaptureId = null;
let voiceChunks = [];
let voiceStarting = false;
let voicePointerHeld = false;
let windowInteraction = null;

function releaseVoiceStream() {
  if (voiceStream) {
    for (const track of voiceStream.getTracks()) track.stop();
  }
  voiceStream = null;
}

async function startVoiceCapture(event) {
  if (voiceRecorder || voiceStarting || event.button !== 2 || !event.ctrlKey || !event.altKey) return;
  voicePointerHeld = true;
  voiceStarting = true;
  event.preventDefault();
  try {
    const config = await ipcRenderer.invoke("presence-input-config");
    if (!config?.enabled || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") return;
    voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!voicePointerHeld) {
      releaseVoiceStream();
      return;
    }
    const captureId = globalThis.crypto?.randomUUID?.() || `capture-${Date.now()}`;
    const started = await ipcRenderer.invoke("presence-input-start", { capture_id: captureId });
    if (!started?.ok) {
      releaseVoiceStream();
      return;
    }
    voiceCaptureId = captureId;
    if (!voicePointerHeld) {
      voiceCaptureId = null;
      await ipcRenderer.invoke("presence-input-cancel");
      releaseVoiceStream();
      return;
    }
    voiceChunks = [];
    const candidates = ["audio/webm;codecs=opus", "audio/webm"];
    const mimeType = candidates.find((item) => MediaRecorder.isTypeSupported(item)) || "";
    voiceRecorder = mimeType
      ? new MediaRecorder(voiceStream, { mimeType })
      : new MediaRecorder(voiceStream);
    voiceRecorder.ondataavailable = (chunk) => {
      if (chunk.data?.size) voiceChunks.push(chunk.data);
    };
    voiceRecorder.start(100);
    if (!voicePointerHeld) await stopVoiceCapture(false);
  } catch (_error) {
    voiceRecorder = null;
    if (voiceCaptureId) {
      voiceCaptureId = null;
      await ipcRenderer.invoke("presence-input-cancel");
    }
    voiceChunks = [];
    releaseVoiceStream();
  } finally {
    voiceStarting = false;
  }
}

async function stopVoiceCapture(discard = false) {
  const recorder = voiceRecorder;
  const captureId = voiceCaptureId;
  voiceRecorder = null;
  voiceCaptureId = null;
  if (!recorder || !captureId) return;
  await new Promise((resolve) => {
    recorder.addEventListener("stop", resolve, { once: true });
    if (recorder.state !== "inactive") recorder.stop();
    else resolve();
  });
  releaseVoiceStream();
  if (discard) {
    voiceChunks = [];
    await ipcRenderer.invoke("presence-input-cancel");
    return;
  }
  const blob = new Blob(voiceChunks, { type: recorder.mimeType || "audio/webm" });
  voiceChunks = [];
  const bytes = await blob.arrayBuffer();
  await ipcRenderer.invoke("presence-input-finish", { capture_id: captureId, bytes });
}

window.addEventListener("DOMContentLoaded", () => {
  ensureInputFeedback();
  renderInputFeedback();
  document.addEventListener("contextmenu", (event) => {
    if (event.ctrlKey && event.altKey) event.preventDefault();
  });
  document.addEventListener("pointerdown", startVoiceCapture, true);
  document.addEventListener("pointerdown", (event) => {
    updatePointerFeedback(event);
    if (event.button !== 0 || !event.ctrlKey || !event.altKey || windowInteraction) return;
    event.preventDefault();
    windowInteraction = { mode: event.shiftKey ? "resize" : "move" };
    setPointerFeedback(windowInteraction.mode);
    ipcRenderer.send("presence-window-interaction", {
      phase: "start",
      mode: windowInteraction.mode,
      x: event.screenX,
      y: event.screenY,
    });
  }, true);
  document.addEventListener("pointermove", (event) => {
    updatePointerFeedback(event);
    if (!windowInteraction) return;
    ipcRenderer.send("presence-window-interaction", {
      phase: "update",
      x: event.screenX,
      y: event.screenY,
    });
  }, true);
  document.addEventListener("pointerup", (event) => {
    if (event.button === 2) {
      voicePointerHeld = false;
      if (voiceRecorder) void stopVoiceCapture(false);
    }
    if (event.button === 0 && windowInteraction) {
      windowInteraction = null;
      ipcRenderer.send("presence-window-interaction", { phase: "end" });
    }
    updatePointerFeedback(event);
  }, true);
  document.addEventListener("pointercancel", () => {
    voicePointerHeld = false;
    if (voiceRecorder) void stopVoiceCapture(true);
    if (windowInteraction) {
      windowInteraction = null;
      ipcRenderer.send("presence-window-interaction", { phase: "end" });
    }
    setPointerFeedback("idle");
  }, true);
  document.addEventListener("pointerenter", updatePointerFeedback, true);
  document.addEventListener("pointerleave", () => {
    if (!windowInteraction) setPointerFeedback("idle");
  }, true);
  document.addEventListener("keyup", (event) => {
    if (event.key === "Control" || event.key === "Alt") {
      voicePointerHeld = false;
      if (voiceRecorder) void stopVoiceCapture(false);
      if (windowInteraction) {
        windowInteraction = null;
        ipcRenderer.send("presence-window-interaction", { phase: "end" });
      }
      setPointerFeedback("idle");
    }
    if (event.key === "Escape") {
      voicePointerHeld = false;
      if (voiceRecorder) void stopVoiceCapture(true);
      setPointerFeedback("idle");
    }
  }, true);
  window.addEventListener("blur", () => {
    voicePointerHeld = false;
    if (voiceRecorder) void stopVoiceCapture(true);
    if (windowInteraction) {
      windowInteraction = null;
      ipcRenderer.send("presence-window-interaction", { phase: "end" });
    }
    setPointerFeedback("idle");
  });
});

ipcRenderer.on("presence-input-state", (_event, state) => {
  setRuntimeFeedback(state?.phase);
});

ipcRenderer.on("presence-input-policy", (_event, policy) => {
  if (!policy?.enabled) {
    voicePointerHeld = false;
    if (voiceRecorder) void stopVoiceCapture(true);
  }
});

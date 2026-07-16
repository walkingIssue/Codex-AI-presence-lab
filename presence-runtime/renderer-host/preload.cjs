"use strict";

const { contextBridge, ipcRenderer } = require("electron");

let snapshotCallback = null;
let eventCallback = null;

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

contextBridge.exposeInMainWorld("presenceRenderer", Object.freeze({
  onSnapshot(callback) {
    if (typeof callback !== "function") throw new TypeError("snapshot callback is required");
    snapshotCallback = callback;
  },
  onEvent(callback) {
    if (typeof callback !== "function") throw new TypeError("event callback is required");
    eventCallback = callback;
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


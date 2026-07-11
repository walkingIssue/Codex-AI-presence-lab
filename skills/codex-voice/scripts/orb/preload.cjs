const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("orbApi", {
  onAudioEvent(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("audio-event", listener);
    return () => ipcRenderer.removeListener("audio-event", listener);
  },
  onMoveMode(callback) {
    const listener = (_event, enabled) => callback(Boolean(enabled));
    ipcRenderer.on("move-mode", listener);
    return () => ipcRenderer.removeListener("move-mode", listener);
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

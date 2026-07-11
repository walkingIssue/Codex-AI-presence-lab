const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("orbApi", {
  onAudioEvent(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("audio-event", listener);
    return () => ipcRenderer.removeListener("audio-event", listener);
  },
  close() {
    ipcRenderer.send("close-orb");
  },
});

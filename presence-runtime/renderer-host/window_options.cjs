"use strict";

function presenceWindowOptions(geometry, preload) {
  return {
    ...geometry,
    show: false,
    transparent: true,
    frame: false,
    resizable: true,
    alwaysOnTop: true,
    hasShadow: false,
    backgroundColor: "#00000000",
    skipTaskbar: false,
    webPreferences: {
      preload,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  };
}

function enforcePresenceWindow(window) {
  window.setAlwaysOnTop(true, "floating");
}

module.exports = { enforcePresenceWindow, presenceWindowOptions };


"use strict";

const COLORS = {
  idle: "120 196 255",
  thinking: "167 139 250",
  tool: "74 222 128",
  skill: "244 114 182",
  cli: "251 191 36",
  waiting: "251 146 60",
  error: "248 113 113",
};

let speaking = false;
let activity = "idle";
let energy = 0.12;

function render() {
  const target = speaking ? Math.max(0.3, energy) : activity === "idle" ? 0.12 : 0.24;
  energy += (target - energy) * 0.18;
  document.documentElement.style.setProperty("--presence-energy", energy.toFixed(3));
  requestAnimationFrame(render);
}

window.presenceRenderer.onSnapshot((snapshot) => {
  activity = snapshot.semantic.activity || "idle";
  document.documentElement.style.setProperty(
    "--presence-color",
    COLORS[activity] || COLORS.idle,
  );
});

window.presenceRenderer.onEvent((event) => {
  if (event.type === "audio") {
    energy = Math.max(energy, Number(event.amplitude) || 0);
  }
  if (event.type === "state" || event.type === "voice-output") {
    speaking = event.state === "speaking" || event.state === "started";
  }
  if (event.type === "activity") {
    activity = event.state || "idle";
    document.documentElement.style.setProperty(
      "--presence-color",
      COLORS[activity] || COLORS.idle,
    );
  }
});

window.presenceRenderer.ready();
requestAnimationFrame(render);


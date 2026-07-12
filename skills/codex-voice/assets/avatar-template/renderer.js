const avatar = document.getElementById("avatar");
let activity = "idle";
let speaking = 0;
let targetEnergy = 0.18;
let energy = targetEnergy;

const colors = {
  idle: "#70e8ff",
  thinking: "#7568ff",
  tool: "#ffc34d",
  skill: "#e879ff",
  cli: "#74e58a",
  waiting: "#6da9d6",
  error: "#ff6e80",
};

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function applyEvent(event) {
  if (!event || typeof event !== "object") return;

  if (event.type === "activity") {
    activity = colors[event.state] ? event.state : "idle";
    targetEnergy = activity === "idle" ? 0.18 : 0.42;
    return;
  }

  if (event.type === "state") {
    speaking = event.state === "speaking" ? 1 : 0;
    if (!speaking) targetEnergy = activity === "idle" ? 0.18 : 0.42;
    return;
  }

  if (event.type === "audio") {
    const amplitude = clamp(Number(event.amplitude) || 0, 0, 1);
    targetEnergy = Math.max(targetEnergy, 0.25 + amplitude * 0.75);
  }
}

window.orbApi.onAudioEvent(applyEvent);
window.orbApi.onMoveMode((enabled) => {
  document.body.classList.toggle("move-mode", enabled);
});

function render(milliseconds) {
  energy += (targetEnergy - energy) * (speaking ? 0.24 : 0.06);
  targetEnergy *= speaking ? 0.985 : 0.995;
  const breathing = Math.sin(milliseconds / 900) * 0.025;
  const scale = 1 + breathing + energy * 0.08;
  avatar.style.setProperty("--presence-color", colors[activity] || colors.idle);
  avatar.style.setProperty("--presence-energy", String(clamp(energy, 0, 1)));
  avatar.style.setProperty("--presence-scale", String(scale));
  requestAnimationFrame(render);
}

requestAnimationFrame(render);

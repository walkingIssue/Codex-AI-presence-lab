const root = document.documentElement;

let activity = "idle";
let speaking = 0;
let amplitude = 0;
let bandEnergy = 0;
let eyeTargetX = 0;
let eyeTargetY = 0;
let eyeX = 0;
let eyeY = 0;
let blink = 0;
let blinkTarget = 0;
let nextBlinkAt = performance.now() + 2600;
let moveMode = false;
let dragging = false;

const colors = {
  idle: "#70e8ff",
  thinking: "#8071ff",
  tool: "#ffc34d",
  skill: "#ef87ff",
  cli: "#7be695",
  waiting: "#78b8df",
  error: "#ff718b",
};

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function average(values) {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function scheduleBlink(now) {
  nextBlinkAt = now + 2400 + Math.random() * 3200;
}

function applyEvent(event) {
  if (!event || typeof event !== "object") return;

  if (event.type === "activity") {
    activity = colors[event.state] ? event.state : "idle";
    return;
  }

  if (event.type === "state") {
    speaking = event.state === "speaking" ? 1 : 0;
    if (!speaking) amplitude *= 0.65;
    return;
  }

  if (event.type === "audio") {
    amplitude = clamp(Number(event.amplitude) || 0, 0, 1);
    const bands = Array.isArray(event.bands)
      ? event.bands.map((value) => clamp(Number(value) || 0, 0, 1))
      : [];
    bandEnergy = average(bands);
    const low = average(bands.slice(0, 5));
    const high = average(bands.slice(-5));
    eyeTargetX = clamp((high - low) * 22, -10, 10);
    eyeTargetY = clamp((bandEnergy - 0.25) * -10, -5, 5);
  }
}

window.orbApi.onAudioEvent(applyEvent);

function moveModifierHeld(event) {
  return Boolean(event.altKey && (event.ctrlKey || event.metaKey));
}

function setMoveMode(enabled) {
  moveMode = Boolean(enabled);
  if (!moveMode) {
    dragging = false;
    document.body.classList.remove("dragging");
  }
  document.body.classList.toggle("move-mode", moveMode);
}

window.orbApi.onMoveMode(setMoveMode);

function beginDrag(event) {
  if (dragging) return;
  dragging = true;
  document.body.classList.add("dragging");
  if (event.pointerId !== undefined) avatar.setPointerCapture(event.pointerId);
  window.orbApi.dragStart({ screenX: event.screenX, screenY: event.screenY });
  event.preventDefault();
}

window.addEventListener("mousemove", (event) => {
  const wantsMove = moveModifierHeld(event);
  if (!moveMode && !dragging && wantsMove) {
    setMoveMode(true);
    window.orbApi.setMoveMode(true);
  } else if (moveMode && dragging) {
    window.orbApi.drag({ screenX: event.screenX, screenY: event.screenY });
  } else if (moveMode && !dragging && !wantsMove) {
    setMoveMode(false);
    window.orbApi.setMoveMode(false);
  }
});

avatar.addEventListener("pointerdown", (event) => {
  if (moveMode && event.button === 0 && moveModifierHeld(event)) beginDrag(event);
});

avatar.addEventListener("pointermove", (event) => {
  if (!moveMode || !dragging) return;
  window.orbApi.drag({ screenX: event.screenX, screenY: event.screenY });
  event.preventDefault();
});

function finishDrag(event) {
  if (!dragging) return;
  dragging = false;
  document.body.classList.remove("dragging");
  if (event && avatar.hasPointerCapture(event.pointerId)) avatar.releasePointerCapture(event.pointerId);
  window.orbApi.dragEnd();
  setMoveMode(false);
  window.orbApi.setMoveMode(false);
}

avatar.addEventListener("pointerup", finishDrag);
avatar.addEventListener("pointercancel", finishDrag);
window.addEventListener("keydown", (event) => {
  if (moveMode && event.key === "Escape") {
    setMoveMode(false);
    window.orbApi.setMoveMode(false);
  }
});

function render(now) {
  if (now >= nextBlinkAt && blinkTarget === 0) {
    blinkTarget = 1;
    scheduleBlink(now);
  } else if (blinkTarget === 1 && blink > 0.82) {
    blinkTarget = 0;
  }

  blink += (blinkTarget - blink) * 0.32;
  eyeX += (eyeTargetX - eyeX) * 0.16;
  eyeY += (eyeTargetY - eyeY) * 0.16;
  amplitude *= speaking ? 0.986 : 0.965;
  bandEnergy *= speaking ? 0.985 : 0.96;

  const breath = Math.sin(now / 1150) * 1.8;
  const activityPulse = activity === "idle" ? 0.0 : 0.035;
  const headTilt = Math.sin(now / 1700) * 1.8 + (highFrequencyBias() * 3.2);
  const headX = Math.sin(now / 2300) * 2.5 + (speaking ? amplitude * 2.5 : 0);
  const mouthOpen = speaking ? clamp(0.08 + amplitude * 1.15 + bandEnergy * 0.24, 0.08, 1) : 0.04;

  root.style.setProperty("--accent", colors[activity] || colors.idle);
  root.style.setProperty("--accent-soft", speaking ? "#efffff" : "#b9f5ff");
  root.style.setProperty("--activity-energy", String(clamp(activityPulse + amplitude * 0.8, 0.12, 1)));
  root.style.setProperty("--head-x", `${headX}px`);
  root.style.setProperty("--head-y", `${breath * 0.18}px`);
  root.style.setProperty("--head-tilt", `${headTilt}deg`);
  root.style.setProperty("--eye-x", `${eyeX}px`);
  root.style.setProperty("--eye-y", `${eyeY}px`);
  root.style.setProperty("--blink", String(clamp(blink, 0, 1)));
  root.style.setProperty("--mouth-open", String(mouthOpen));
  root.style.setProperty("--body-tilt", `${Math.sin(now / 2600) * 0.8 + activityPulse * 12}deg`);
  root.style.setProperty("--body-x", `${Math.sin(now / 1900) * 1.8}px`);
  root.style.setProperty("--body-y", `${breath}px`);
  requestAnimationFrame(render);
}

function highFrequencyBias() {
  return clamp((eyeTargetX / 10) * 0.5, -0.5, 0.5);
}

requestAnimationFrame(render);

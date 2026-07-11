const canvas = document.getElementById("orb");
const moveHint = document.getElementById("move-hint");
const gl = canvas.getContext("webgl2", {
  alpha: true,
  antialias: true,
  premultipliedAlpha: false,
  powerPreference: "high-performance",
});

if (!gl) {
  throw new Error("WebGL2 is required for the Strand Orb renderer.");
}

const vertexSource = `#version 300 es
in vec2 a_position;
out vec2 v_uv;

void main() {
  v_uv = a_position * 0.5 + 0.5;
  gl_Position = vec4(a_position, 0.0, 1.0);
}`;

const fragmentSource = `#version 300 es
precision highp float;

in vec2 v_uv;
out vec4 out_color;

uniform vec2 u_resolution;
uniform float u_time;
uniform float u_amplitude;
uniform float u_speaking;
uniform float u_cadence;
uniform float u_bands[16];
uniform vec3 u_activity_color;
uniform float u_activity_energy;
uniform float u_activity_kind;
uniform float u_activity_node_bounce;

const float PI = 3.14159265359;
const float TAU = 6.28318530718;

float hash21(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float gaussian(float x, float width) {
  return exp(-x * x / max(width, 0.00001));
}

void main() {
  vec2 p = (v_uv - 0.5) * 2.0;
  p.x *= u_resolution.x / u_resolution.y;

  float time = u_time * 0.72;
  float voice = smoothstep(0.015, 0.95, u_amplitude);
  float impact = clamp(u_amplitude * 1.35 + pow(u_amplitude, 1.6) * 0.85, 0.0, 1.6);
  float geometryImpact = clamp(u_amplitude * 0.62 + pow(u_amplitude, 1.6) * 0.32, 0.0, 0.9);
  float cadence = clamp(u_cadence, 0.0, 1.0);
  float radius = length(p);
  float angle = atan(p.y, p.x);

  float thinkingState = 1.0 - step(0.5, abs(u_activity_kind - 1.0));
  float toolState = 1.0 - step(0.5, abs(u_activity_kind - 2.0));
  float skillState = 1.0 - step(0.5, abs(u_activity_kind - 3.0));
  float cliState = 1.0 - step(0.5, abs(u_activity_kind - 4.0));
  float waitingState = 1.0 - step(0.5, abs(u_activity_kind - 5.0));
  float errorState = 1.0 - step(0.5, abs(u_activity_kind - 6.0));
  float speechSuppression = 1.0 - smoothstep(0.05, 0.85, u_speaking);
  float activityEnergy = clamp(u_activity_energy, 0.0, 1.0) * speechSuppression;

  float angularPosition = fract(angle / TAU + 0.5);
  float spectral = 0.0;
  for (int i = 0; i < 16; i++) {
    float center = (float(i) + 0.5) / 16.0;
    float distanceToBand = abs(angularPosition - center);
    distanceToBand = min(distanceToBand, 1.0 - distanceToBand);
    float bandShape = 1.0 - smoothstep(0.0, 0.095, distanceToBand);
    spectral += u_bands[i] * bandShape;
  }
  spectral = clamp(spectral, 0.0, 1.0);
  float cadenceWave = 0.5 + 0.5 * sin(radius * 34.0 - time * 9.0);
  float cadenceRipple = cadence * cadenceWave;
  float cadenceLight = cadence * 0.45;
  float cadenceLightRipple = cadenceLight * cadenceWave;
  float cadenceAsymmetry = cadence * sin(angle * 2.0 - time * 1.0);
  float volumeLight = voice * 0.55;
  float impactLight = impact * 0.62;

  float thinkingPulse = 0.5 + 0.5 * sin(time * 1.35) + 0.08 * sin(time * 2.4 + 1.0);
  float toolPulse = 0.5 + 0.5 * sin(time * 4.4 + sin(angle * 3.0) * 0.55);
  float skillPulse = 0.5 + 0.5 * sin(time * 2.15 + 0.7);
  float cliPulse = 0.5 + 0.5 * sin(time * 3.1 + angle * 2.0);
  float waitingPulse = 0.5 + 0.5 * sin(time * 0.72);
  float errorPulse = 0.5 + 0.5 * sin(time * 8.0);
  float activityPulse = clamp(
    thinkingState * thinkingPulse
      + toolState * toolPulse
      + skillState * skillPulse
      + cliState * cliPulse
      + waitingState * waitingPulse
      + errorState * errorPulse,
    0.0,
    1.0
  );
  float activityGeometry = activityEnergy * (
    thinkingState * (0.010 + 0.010 * activityPulse) * sin(angle * 5.0 + time * 0.8)
      + toolState * (0.014 + 0.016 * activityPulse) * sin(angle * 9.0 - time * 3.2)
      + skillState * (0.012 + 0.010 * activityPulse) * sin(angle * 3.0 + time * 1.2)
      + cliState * (0.013 + 0.013 * activityPulse) * sin(angle * 11.0 + time * 2.5)
      + waitingState * 0.006 * sin(angle * 2.0 - time * 0.6)
      + errorState * 0.028 * sin(angle * 13.0 - time * 9.0)
  );
  float activityLight = activityEnergy * (0.22 + 0.58 * activityPulse);

  float idleBreath = 0.5 + 0.5 * sin(time * 2.4) + 0.12 * sin(time * 5.1 + 1.4);
  float breathing = 0.018 * sin(time * 1.4) + 0.012 * sin(time * 2.1 + 1.7);
  float ringRadius = 0.57 + breathing
    + 0.038 * sin(angle * 13.0 + time * 1.8)
    + 0.022 * sin(angle * 37.0 - time * 2.6)
    + geometryImpact * (0.040 + 0.024 * sin(angle * 9.0 + time * 4.0))
    + u_speaking * spectral * (0.022 + geometryImpact * 0.058)
    + u_speaking * geometryImpact * 0.015 * sin(angle * 5.0 + time * 11.0)
    + cadence * (0.016 + 0.022 * cadenceWave) * sin(angle * 7.0 - time * 5.0)
    + cadenceRipple * 0.022 * sin(angle * 11.0 + time * 3.0)
    + cadenceAsymmetry * 0.050
    + activityGeometry;

  float ringDistance = abs(radius - ringRadius);
  float ring = gaussian(ringDistance, 0.0015 + voice * 0.0065);

  float sectors = 104.0;
  float sector = abs(fract(angle / TAU * sectors + 0.5) - 0.5);
  float strandWidth = 0.010 + voice * 0.030;
  float strands = 1.0 - smoothstep(0.0, strandWidth, sector);
  float strandReach = 0.68 + voice * 0.10
    + u_speaking * spectral * (0.06 + geometryImpact * 0.04)
    + cadence * (0.040 + cadenceRipple * 0.065)
    + cadenceAsymmetry * 0.050
    + activityGeometry * 1.15;
  float strandEnd = 0.88 + u_speaking * spectral * 0.03 + geometryImpact * 0.012
    + cadence * 0.020
    + cadenceAsymmetry * 0.020;
  // Keep smoothstep's edges ordered at high cadence values so strands bend
  // instead of splitting when the asymmetric reach is strongly activated.
  strandReach = min(strandReach, strandEnd - 0.060);
  float strandWindow = smoothstep(0.27, 0.51, radius)
    * (1.0 - smoothstep(strandReach, strandEnd, radius));

  float filamentWave = 0.5 + 0.5 * sin(angle * 29.0 - time * 9.0);
  float filament = strands * strandWindow * (0.16 + 0.84 * filamentWave)
    * (0.55 + impactLight * 1.10 + u_speaking * spectral * 1.35
      + cadenceLight * (0.20 + cadenceWave * 0.55));

  float secondaryRadius = 0.47
    + 0.026 * sin(angle * 21.0 - time * 2.2)
    + geometryImpact * 0.022
    + cadence * (0.009 + cadenceWave * 0.015)
    + cadenceAsymmetry * 0.030
    + activityGeometry * 0.70;
  float secondary = gaussian(abs(radius - secondaryRadius), 0.0020 + voice * 0.0045)
    * (0.22 + 0.78 * (1.0 - strands));
  float innerCircleRadius = 0.33 + 0.010 * sin(angle * 11.0 + time * 1.3);
  float innerCircle = gaussian(abs(radius - innerCircleRadius), 0.0026 + voice * 0.0035)
    * (0.55 + 0.45 * (1.0 - strands));
  float playbackInnerCircle = innerCircle * mix(1.0, 0.30, clamp(u_speaking, 0.0, 1.0));
  float innerHaloRadius = 0.245
    + 0.014 * sin(angle * 8.0 - time * 1.7)
    + cadence * (0.010 + 0.012 * cadenceWave) * sin(angle * 5.0 - time * 4.0)
    + cadenceAsymmetry * 0.020;
  float innerHalo = gaussian(abs(radius - innerHaloRadius), 0.035 + voice * 0.014)
    * (0.07 + u_speaking * 0.12 + cadenceLight * 0.20 + activityLight * 0.28);
  float activityHalo = gaussian(
    abs(radius - (0.39 + 0.012 * sin(angle * 4.0 + time * 0.9))),
    0.020 + activityEnergy * 0.014
  ) * activityLight;
  float nodeBounce = clamp(u_activity_node_bounce, -1.0, 1.0);
  float nodeRadius = max(
    0.012,
    0.034 + activityEnergy * (0.012 + 0.010 * activityPulse) + nodeBounce * 0.018
  );
  float activityNode = (1.0 - smoothstep(nodeRadius * 0.72, nodeRadius, radius))
    * activityEnergy * (0.60 + 0.40 * activityPulse);
  float activityNodeEdge = gaussian(
    abs(radius - nodeRadius),
    0.0012 + activityEnergy * 0.003
  ) * activityEnergy * (0.35 + 0.65 * activityPulse);

  float core = gaussian(radius, 0.045 + voice * 0.18) * (0.62 + 0.38 * idleBreath);
  float halo = gaussian(max(radius - 0.10, 0.0), 0.14 + voice * 0.16) * (0.20 + volumeLight * 0.30);
  float shimmer = pow(max(0.0, sin(angle * 17.0 + time * 3.0)), 18.0)
    * gaussian(radius - ringRadius, 0.020 + voice * 0.035) * (0.25 + impactLight * 1.4);
  float shock = pow(max(0.0, sin(angle * 7.0 - time * 8.0)), 12.0)
    * gaussian(radius - ringRadius, 0.045) * (geometryImpact + cadenceLight * 0.22);

  vec3 cyan = vec3(0.12, 0.88, 1.0);
  vec3 violet = vec3(0.46, 0.22, 1.0);
  vec3 white = vec3(0.82, 0.98, 1.0);
  vec3 color = mix(cyan, violet, 0.34 + 0.22 * sin(time * 0.7));
  color = mix(color, white, clamp(core * 0.70 + shimmer * 0.28 + playbackInnerCircle * 0.16 + innerHalo * 0.24, 0.0, 0.85));
  float activityTint = activityEnergy * (0.20 + 0.34 * activityPulse);
  color = mix(color, u_activity_color, clamp(activityTint + activityNode * 0.62, 0.0, 0.80));

  float baselineStructure = ring * (0.58 + 0.98 * strands) + filament * 0.82
    + secondary * 0.60 + innerCircle * 0.76 + shimmer * 0.40 + core * 0.28 + halo * 0.20
    + activityHalo * 0.72 + activityNode * 0.95 + activityNodeEdge * 0.40;
  float activeStructure = ring * (0.54 + 1.02 * strands) + filament * 0.84
    + secondary * 0.54 + playbackInnerCircle * 0.34 + shimmer * 0.62 + shock * 0.54
    + cadenceLightRipple * 0.18 + innerHalo * 0.70 + core * 0.22 + halo * 0.14
    + activityHalo * 0.60 + activityNode * 0.72 + activityNodeEdge * 0.28;
  float activeMix = clamp(max(u_speaking * 1.25, voice * 0.98), 0.0, 1.0);
  float structure = mix(baselineStructure, activeStructure, activeMix);
  float alpha = clamp(structure * (1.02 + volumeLight * 0.75), 0.0, 0.98);

  out_color = vec4(color, alpha);
}`;

function compile(type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader));
  }
  return shader;
}

const program = gl.createProgram();
gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSource));
gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSource));
gl.linkProgram(program);
if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
  throw new Error(gl.getProgramInfoLog(program));
}

const vertices = new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]);
const buffer = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);
const position = gl.getAttribLocation(program, "a_position");
gl.enableVertexAttribArray(position);
gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

const uniforms = {
  resolution: gl.getUniformLocation(program, "u_resolution"),
  time: gl.getUniformLocation(program, "u_time"),
  amplitude: gl.getUniformLocation(program, "u_amplitude"),
  speaking: gl.getUniformLocation(program, "u_speaking"),
  cadence: gl.getUniformLocation(program, "u_cadence"),
  bands: gl.getUniformLocation(program, "u_bands[0]"),
  activityColor: gl.getUniformLocation(program, "u_activity_color"),
  activityEnergy: gl.getUniformLocation(program, "u_activity_energy"),
  activityKind: gl.getUniformLocation(program, "u_activity_kind"),
  activityNodeBounce: gl.getUniformLocation(program, "u_activity_node_bounce"),
};

let targetAmplitude = 0;
let amplitude = 0;
let speaking = 0;
let targetSpeaking = 0;
let targetCadence = 0;
let cadence = 0;
let lastAudioAmplitude = 0;
let lastBandEnergy = 0;
const targetBands = new Float32Array(16);
const bands = new Float32Array(16);
const ACTIVITY_STATES = ["idle", "thinking", "tool", "skill", "cli", "waiting", "error"];
const ACTIVITY_COLORS = {
  idle: [0.12, 0.88, 1.0],
  thinking: [0.34, 0.24, 1.0],
  tool: [1.0, 0.56, 0.12],
  skill: [0.90, 0.20, 0.96],
  cli: [0.20, 1.0, 0.48],
  waiting: [0.20, 0.60, 1.0],
  error: [1.0, 0.16, 0.18],
};
const ACTIVITY_ENERGY = {
  idle: 0.0,
  thinking: 0.62,
  tool: 0.76,
  skill: 0.68,
  cli: 0.72,
  waiting: 0.34,
  error: 0.90,
};
let moveMode = false;
let dragging = false;
let activityKind = 0;
let targetActivityEnergy = 0;
let activityEnergy = 0;
let activityExpiresAt = 0;
let activityNodeBounce = 0;
let activityNodeVelocity = 0;
let lastFrameMilliseconds = 0;
const targetActivityColor = new Float32Array(ACTIVITY_COLORS.idle);
const activityColor = new Float32Array(ACTIVITY_COLORS.idle);

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
  moveHint.hidden = !moveMode;
}

window.orbApi.onMoveMode(setMoveMode);

function beginDrag(event) {
  if (dragging) {
    return;
  }
  dragging = true;
  document.body.classList.add("dragging");
  if (event.pointerId !== undefined) {
    canvas.setPointerCapture(event.pointerId);
  }
  window.orbApi.dragStart({ screenX: event.screenX, screenY: event.screenY });
  event.preventDefault();
}

// Electron forwards mouse movement while the transparent window is
// click-through. Use that safe signal to arm the window before the deliberate
// Ctrl/Cmd+Alt+left-button gesture arrives. Forwarded mousemove events do not
// reliably preserve `buttons`, so the left-button check belongs on pointerdown.
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

canvas.addEventListener("pointerdown", (event) => {
  if (!moveMode || event.button !== 0 || !moveModifierHeld(event)) {
    return;
  }
  beginDrag(event);
});

canvas.addEventListener("pointermove", (event) => {
  if (!moveMode || !dragging) {
    return;
  }
  window.orbApi.drag({ screenX: event.screenX, screenY: event.screenY });
  event.preventDefault();
});

function finishDrag(event) {
  if (!dragging) {
    return;
  }
  dragging = false;
  document.body.classList.remove("dragging");
  if (event && canvas.hasPointerCapture(event.pointerId)) {
    canvas.releasePointerCapture(event.pointerId);
  }
  window.orbApi.dragEnd();
  setMoveMode(false);
  window.orbApi.setMoveMode(false);
}

canvas.addEventListener("pointerup", finishDrag);
canvas.addEventListener("pointercancel", finishDrag);
window.addEventListener("keydown", (event) => {
  if (moveMode && event.key === "Escape") {
    setMoveMode(false);
    window.orbApi.setMoveMode(false);
  }
});

function setActivityState(state, ttlMs = 0) {
  const normalized = ACTIVITY_STATES.includes(state) ? state : "idle";
  const previous = ACTIVITY_STATES[activityKind];
  if (normalized !== previous) {
    activityNodeVelocity += normalized === "idle" ? -3.6 : 5.5;
  }
  activityKind = ACTIVITY_STATES.indexOf(normalized);
  targetActivityEnergy = ACTIVITY_ENERGY[normalized];
  const nextColor = ACTIVITY_COLORS[normalized];
  for (let index = 0; index < targetActivityColor.length; index += 1) {
    targetActivityColor[index] = nextColor[index];
  }
  activityExpiresAt = normalized === "idle" || ttlMs <= 0
    ? 0
    : performance.now() + Math.max(500, Math.min(30000, Number(ttlMs) || 0));
}

window.orbApi.onAudioEvent((event) => {
  if (event.type === "activity") {
    setActivityState(event.state, Number(event.ttl_ms) || 0);
    return;
  }
  if (event.type === "state") {
    targetSpeaking = event.state === "speaking" ? 1 : 0;
    if (targetSpeaking) {
      targetAmplitude = Math.max(targetAmplitude, 0.18);
    } else {
      targetCadence = 0;
      lastAudioAmplitude = 0;
      lastBandEnergy = 0;
    }
    return;
  }
  if (event.type === "audio") {
    const nextAmplitude = Math.max(0, Math.min(1, (Number(event.amplitude) || 0) * 1.35));
    let nextBandEnergy = 0;
    if (Array.isArray(event.bands)) {
      for (let index = 0; index < targetBands.length; index += 1) {
        targetBands[index] = Math.max(0, Math.min(1, Number(event.bands[index]) || 0));
        nextBandEnergy += targetBands[index];
      }
    }
    nextBandEnergy /= targetBands.length;
    const amplitudeRise = Math.max(0, nextAmplitude - lastAudioAmplitude);
    const bandRise = Math.max(0, nextBandEnergy - lastBandEnergy);
    targetCadence = Math.max(
      targetCadence,
      Math.min(1, amplitudeRise * 4.8 + bandRise * 2.6),
    );
    lastAudioAmplitude = nextAmplitude;
    lastBandEnergy = nextBandEnergy;
    targetAmplitude = nextAmplitude;
    targetSpeaking = 1;
  }
});

function resize() {
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(canvas.clientWidth * scale));
  const height = Math.max(1, Math.floor(canvas.clientHeight * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    gl.viewport(0, 0, width, height);
  }
}

function render(milliseconds) {
  resize();
  const deltaSeconds = lastFrameMilliseconds === 0
    ? 1 / 60
    : Math.min(0.05, Math.max(0.001, (milliseconds - lastFrameMilliseconds) / 1000));
  lastFrameMilliseconds = milliseconds;
  if (activityExpiresAt > 0 && performance.now() >= activityExpiresAt) {
    setActivityState("idle");
  }
  // Keep the attack lively, but let the natural signal decay carry the release.
  amplitude += (targetAmplitude - amplitude) * (targetSpeaking ? 0.32 : 0.05);
  targetAmplitude *= targetSpeaking ? 0.985 : 0.992;
  speaking += (targetSpeaking - speaking) * (targetSpeaking ? 0.16 : 0.032);
  cadence += (targetCadence - cadence) * (targetSpeaking ? 0.34 : 0.055);
  targetCadence *= targetSpeaking ? 0.88 : 0.90;
  for (let index = 0; index < bands.length; index += 1) {
    bands[index] += (targetBands[index] - bands[index]) * (targetSpeaking ? 0.26 : 0.05);
    targetBands[index] *= targetSpeaking ? 0.995 : 0.98;
  }
  activityNodeVelocity += (-activityNodeBounce * 88 - activityNodeVelocity * 15) * deltaSeconds;
  activityNodeBounce += activityNodeVelocity * deltaSeconds;
  activityNodeBounce = Math.max(-1, Math.min(1, activityNodeBounce));
  activityEnergy += (targetActivityEnergy - activityEnergy) * (targetActivityEnergy > activityEnergy ? 0.10 : 0.045);
  for (let index = 0; index < activityColor.length; index += 1) {
    activityColor[index] += (targetActivityColor[index] - activityColor[index]) * 0.075;
  }

  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.useProgram(program);
  gl.uniform2f(uniforms.resolution, canvas.width, canvas.height);
  gl.uniform1f(uniforms.time, milliseconds / 1000);
  gl.uniform1f(uniforms.amplitude, amplitude);
  gl.uniform1f(uniforms.speaking, speaking);
  gl.uniform1f(uniforms.cadence, cadence);
  gl.uniform1fv(uniforms.bands, bands);
  gl.uniform3fv(uniforms.activityColor, activityColor);
  gl.uniform1f(uniforms.activityEnergy, activityEnergy);
  gl.uniform1f(uniforms.activityKind, activityKind);
  gl.uniform1f(uniforms.activityNodeBounce, activityNodeBounce);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  requestAnimationFrame(render);
}

window.addEventListener("resize", resize);
requestAnimationFrame(render);

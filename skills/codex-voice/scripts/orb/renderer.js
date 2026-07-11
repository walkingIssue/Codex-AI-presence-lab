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
uniform float u_bands[16];

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
  float radius = length(p);
  float angle = atan(p.y, p.x);

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

  float idleBreath = 0.5 + 0.5 * sin(time * 2.4) + 0.12 * sin(time * 5.1 + 1.4);
  float breathing = 0.018 * sin(time * 1.4) + 0.012 * sin(time * 2.1 + 1.7);
  float ringRadius = 0.57 + breathing
    + 0.038 * sin(angle * 13.0 + time * 1.8)
    + 0.022 * sin(angle * 37.0 - time * 2.6)
    + geometryImpact * (0.040 + 0.024 * sin(angle * 9.0 + time * 4.0))
    + u_speaking * spectral * (0.022 + geometryImpact * 0.058)
    + u_speaking * geometryImpact * 0.015 * sin(angle * 5.0 + time * 11.0);

  float ringDistance = abs(radius - ringRadius);
  float ring = gaussian(ringDistance, 0.0015 + voice * 0.0065);

  float sectors = 104.0;
  float sector = abs(fract(angle / TAU * sectors + 0.5) - 0.5);
  float strandWidth = 0.010 + voice * 0.030;
  float strands = 1.0 - smoothstep(0.0, strandWidth, sector);
  float strandReach = 0.68 + voice * 0.10
    + u_speaking * spectral * (0.06 + geometryImpact * 0.04);
  float strandEnd = 0.88 + u_speaking * spectral * 0.03 + geometryImpact * 0.012;
  float strandWindow = smoothstep(0.27, 0.51, radius)
    * (1.0 - smoothstep(strandReach, strandEnd, radius));

  float filamentWave = 0.5 + 0.5 * sin(angle * 29.0 - time * 9.0);
  float filament = strands * strandWindow * (0.16 + 0.84 * filamentWave)
    * (0.55 + impact * 1.10 + u_speaking * spectral * 1.35);

  float secondaryRadius = 0.47
    + 0.026 * sin(angle * 21.0 - time * 2.2)
    + geometryImpact * 0.022;
  float secondary = gaussian(abs(radius - secondaryRadius), 0.0020 + voice * 0.0045)
    * (0.22 + 0.78 * (1.0 - strands));
  float innerCircleRadius = 0.33 + 0.010 * sin(angle * 11.0 + time * 1.3);
  float innerCircle = gaussian(abs(radius - innerCircleRadius), 0.0026 + voice * 0.0035)
    * (0.55 + 0.45 * (1.0 - strands));
  float playbackInnerCircle = innerCircle * mix(1.0, 0.30, clamp(u_speaking, 0.0, 1.0));

  float core = gaussian(radius, 0.045 + voice * 0.18) * (0.62 + 0.38 * idleBreath);
  float halo = gaussian(max(radius - 0.10, 0.0), 0.14 + voice * 0.16) * (0.20 + voice * 0.30);
  float shimmer = pow(max(0.0, sin(angle * 17.0 + time * 3.0)), 18.0)
    * gaussian(radius - ringRadius, 0.020 + voice * 0.035) * (0.25 + impact * 1.4);
  float shock = pow(max(0.0, sin(angle * 7.0 - time * 8.0)), 12.0)
    * gaussian(radius - ringRadius, 0.045) * geometryImpact;

  vec3 cyan = vec3(0.12, 0.88, 1.0);
  vec3 violet = vec3(0.46, 0.22, 1.0);
  vec3 white = vec3(0.82, 0.98, 1.0);
  vec3 color = mix(cyan, violet, 0.34 + 0.22 * sin(time * 0.7));
  color = mix(color, white, clamp(core * 0.70 + shimmer * 0.28 + playbackInnerCircle * 0.16, 0.0, 0.85));

  float baselineStructure = ring * (0.58 + 0.98 * strands) + filament * 0.82
    + secondary * 0.60 + innerCircle * 0.76 + shimmer * 0.40 + core * 0.28 + halo * 0.20;
  float activeStructure = ring * (0.54 + 1.02 * strands) + filament * 0.84
    + secondary * 0.54 + playbackInnerCircle * 0.34 + shimmer * 0.62 + shock * 0.54 + core * 0.22 + halo * 0.14;
  float activeMix = clamp(max(u_speaking * 1.25, voice * 0.98), 0.0, 1.0);
  float structure = mix(baselineStructure, activeStructure, activeMix);
  float alpha = clamp(structure * (1.02 + voice * 0.75), 0.0, 0.98);

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
  bands: gl.getUniformLocation(program, "u_bands[0]"),
};

let targetAmplitude = 0;
let amplitude = 0;
let speaking = 0;
let targetSpeaking = 0;
const targetBands = new Float32Array(16);
const bands = new Float32Array(16);
let moveMode = false;
let dragging = false;

function moveModifierHeld(event) {
  return Boolean(event.altKey && (event.ctrlKey || event.metaKey));
}

function setMoveMode(enabled) {
  moveMode = Boolean(enabled);
  dragging = false;
  document.body.classList.toggle("move-mode", moveMode);
  document.body.classList.remove("dragging");
  moveHint.hidden = !moveMode;
}

window.orbApi.onMoveMode(setMoveMode);

// Electron forwards mouse movement while the transparent window is
// click-through. Use that safe signal to arm the window before the deliberate
// Ctrl/Cmd+Alt+left-button gesture arrives.
window.addEventListener("mousemove", (event) => {
  const wantsMove = moveModifierHeld(event);
  if (!moveMode && !dragging && wantsMove) {
    window.orbApi.setMoveMode(true);
  } else if (moveMode && !dragging && !wantsMove) {
    window.orbApi.setMoveMode(false);
  }
});

canvas.addEventListener("pointerdown", (event) => {
  if (!moveMode || event.button !== 0 || !moveModifierHeld(event)) {
    return;
  }
  dragging = true;
  document.body.classList.add("dragging");
  canvas.setPointerCapture(event.pointerId);
  window.orbApi.dragStart({ screenX: event.screenX, screenY: event.screenY });
  event.preventDefault();
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
  window.orbApi.setMoveMode(false);
}

canvas.addEventListener("pointerup", finishDrag);
canvas.addEventListener("pointercancel", finishDrag);
window.addEventListener("keydown", (event) => {
  if (moveMode && event.key === "Escape") {
    window.orbApi.setMoveMode(false);
  }
});

window.orbApi.onAudioEvent((event) => {
  if (event.type === "state") {
    targetSpeaking = event.state === "speaking" ? 1 : 0;
    if (targetSpeaking) {
      targetAmplitude = Math.max(targetAmplitude, 0.18);
    }
    return;
  }
  if (event.type === "audio") {
    targetAmplitude = Math.max(0, Math.min(1, (Number(event.amplitude) || 0) * 1.35));
    if (Array.isArray(event.bands)) {
      for (let index = 0; index < targetBands.length; index += 1) {
        targetBands[index] = Math.max(0, Math.min(1, Number(event.bands[index]) || 0));
      }
    }
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
  amplitude += (targetAmplitude - amplitude) * 0.32;
  targetAmplitude *= targetSpeaking ? 0.985 : 0.96;
  speaking += (targetSpeaking - speaking) * 0.16;
  for (let index = 0; index < bands.length; index += 1) {
    bands[index] += (targetBands[index] - bands[index]) * 0.26;
    targetBands[index] *= targetSpeaking ? 0.995 : 0.94;
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
  gl.uniform1fv(uniforms.bands, bands);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  requestAnimationFrame(render);
}

window.addEventListener("resize", resize);
requestAnimationFrame(render);

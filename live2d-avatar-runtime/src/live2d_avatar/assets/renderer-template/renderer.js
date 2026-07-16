(() => {
  "use strict";

  const config = window.__LIVE2D_AVATAR_CAPABILITIES__;
  if (!config || typeof config !== "object") {
    throw new Error("Live2D avatar capabilities did not load");
  }
  if (config.state_semantics !== "active-toggle-set") {
    throw new Error("Live2D avatar does not declare active-toggle-set semantics");
  }

  const FALLBACK_WIDTH = 440;
  const FALLBACK_HEIGHT = 440;
  const RENDER_RESOLUTION = Math.min(window.devicePixelRatio || 1, 2);
  const HALO_UPDATE_INTERVAL_MS = 50;
  const HALO_ENERGY_EPSILON = 0.01;
  const TWO_PI = Math.PI * 2;
  const OPERATION_SET = 0;
  const OPERATION_ADD = 1;
  const OPERATION_MULTIPLY = 2;
  const avatar = document.getElementById("avatar");
  const canvas = document.getElementById("avatar-canvas");
  const colors = {
    idle: "#70e8ff",
    thinking: "#8071ff",
    tool: "#ffc34d",
    skill: "#ef87ff",
    cli: "#7be695",
    waiting: "#78b8df",
    error: "#ff718b",
  };
  const actionsInReplayOrder = Array.isArray(config.actions)
    ? config.actions
      .filter((action) => action && typeof action.id === "string")
      .map((action) => ({
        id: action.id,
        operations: Array.isArray(action.parameter_operations)
          ? action.parameter_operations.map(compileOperation).filter(Boolean)
          : [],
      }))
    : [];
  const actionsById = new Map(actionsInReplayOrder.map((action) => [action.id, action]));
  const safeDefaults = Array.isArray(config.safe_default_operations)
    ? config.safe_default_operations.map(compileOperation).filter(Boolean)
    : [];
  const baseInitialActions = Array.isArray(config.initial_actions)
    ? config.initial_actions.filter((actionId) => actionsById.has(actionId))
    : [];
  const renderer = config.renderer && typeof config.renderer === "object" ? config.renderer : {};
  const halo = renderer.halo && typeof renderer.halo === "object" ? renderer.halo : {};
  const haloEnabled = halo.enabled !== false;
  const baseActivityActions = renderer.activity_actions && typeof renderer.activity_actions === "object"
    ? renderer.activity_actions
    : {};
  const fixedParameters = Array.isArray(renderer.fixed_parameters)
    ? renderer.fixed_parameters
      .map((fixed) => {
        const parameterId = typeof fixed?.parameter_id === "string" ? fixed.parameter_id : null;
        const value = Number(fixed?.value);
        return parameterId && Number.isFinite(value) ? { parameterId, value, parameterIndex: -1 } : null;
      })
      .filter(Boolean)
    : [];
  const fixedParts = Array.isArray(renderer.fixed_parts)
    ? renderer.fixed_parts
      .map((fixed) => {
        const partId = typeof fixed?.part_id === "string" ? fixed.part_id : null;
        const opacity = Number(fixed?.opacity);
        return partId && Number.isFinite(opacity) ? { partId, opacity, partIndex: -1 } : null;
      })
      .filter(Boolean)
    : [];
  const speechMotion = renderer.speech_motion && typeof renderer.speech_motion === "object"
    ? renderer.speech_motion
    : {};
  const speechMotionTargets = Array.isArray(speechMotion.targets)
    ? speechMotion.targets
      .map((target) => {
        const parameterId = typeof target?.parameter_id === "string" ? target.parameter_id : null;
        const idleGain = Number(target?.idle_gain);
        const speechGain = Number(target?.speech_gain);
        const frequency = Number(target?.frequency);
        const phase = Number(target?.phase);
        if (!parameterId || !Number.isFinite(idleGain) || !Number.isFinite(speechGain)
          || !Number.isFinite(frequency) || !Number.isFinite(phase)) return null;
        return { parameterId, idleGain, speechGain, angularFrequency: frequency * TWO_PI, phase, parameterIndex: -1 };
      })
      .filter(Boolean)
    : [];
  const mouthMotion = (() => {
    const mouth = speechMotion.mouth;
    if (!mouth || typeof mouth !== "object") return null;
    const primaryParameterId = typeof mouth.primary_parameter_id === "string" ? mouth.primary_parameter_id : null;
    const secondaryParameterId = typeof mouth.secondary_parameter_id === "string" ? mouth.secondary_parameter_id : null;
    const jawParameterId = typeof mouth.jaw_parameter_id === "string" ? mouth.jaw_parameter_id : null;
    const baseOpen = Number(mouth.base_open ?? 0);
    const mouthGain = Number(mouth.mouth_gain ?? 0.6);
    const secondaryGain = Number(mouth.secondary_gain ?? 0);
    const jawGain = Number(mouth.jaw_gain ?? 0);
    const attack = Number(mouth.attack ?? 0.22);
    const release = Number(mouth.release ?? 0.1);
    if (!primaryParameterId || !Number.isFinite(baseOpen) || !Number.isFinite(mouthGain)
      || !Number.isFinite(secondaryGain) || !Number.isFinite(jawGain)
      || !Number.isFinite(attack) || !Number.isFinite(release)) return null;
    return { primaryParameterId, secondaryParameterId, jawParameterId, baseOpen, mouthGain, secondaryGain, jawGain, attack, release };
  })();
  const eyelidMotion = (() => {
    const eyelids = speechMotion.eyelids;
    if (!eyelids || typeof eyelids !== "object") return null;
    const leftParameterId = typeof eyelids.left_parameter_id === "string" ? eyelids.left_parameter_id : null;
    const rightParameterId = typeof eyelids.right_parameter_id === "string" ? eyelids.right_parameter_id : null;
    const idleOpenMin = Number(eyelids.idle_open_min ?? eyelids.rest_open);
    const idleOpenMax = Number(eyelids.idle_open_max ?? eyelids.rest_open);
    const idleFrequency = Number(eyelids.idle_frequency ?? 0.2);
    const speechOpen = Number(eyelids.speech_open);
    const wakeGain = Number(eyelids.wake_gain ?? 1);
    const attack = Number(eyelids.attack ?? 0.18);
    const release = Number(eyelids.release ?? 0.1);
    const talkingWakeFloor = Number(eyelids.talking_wake_floor ?? 0);
    if (!leftParameterId || !rightParameterId || !Number.isFinite(idleOpenMin) || !Number.isFinite(idleOpenMax)
      || !Number.isFinite(idleFrequency) || !Number.isFinite(speechOpen) || !Number.isFinite(wakeGain)
      || !Number.isFinite(attack) || !Number.isFinite(release) || !Number.isFinite(talkingWakeFloor)
      || idleOpenMin > idleOpenMax) return null;
    return { leftParameterId, rightParameterId, idleOpenMin, idleOpenMax, idleFrequency, speechOpen, wakeGain, attack, release, talkingWakeFloor };
  })();
  const avatarScale = Number.isFinite(Number(renderer.scale)) ? Number(renderer.scale) : 1;
  const bottomInset = Number.isFinite(Number(renderer.bottom_inset)) ? Number(renderer.bottom_inset) : 6;

  let activity = "idle";
  let speaking = false;
  let amplitude = 0;
  let bandEnergy = 0;
  let speechEnergy = 0;
  let eyelidEnergy = 0;
  let mouthEnergy = 0;
  let app;
  let model;
  let coreModel;
  let viewportWidth = FALLBACK_WIDTH;
  let viewportHeight = FALLBACK_HEIGHT;
  let baseX = viewportWidth / 2;
  let baseY = viewportHeight - 6;
  let activeActionIds = new Set(baseInitialActions);
  let activityActions = mergeActivityActions(baseActivityActions, {});
  let activityActionIds = new Set();
  let suppressedActionIds = new Set();
  let avatarStateApplied = false;
  let activityExpiresAt = 0;
  let composedOperations = [];
  const parameterIndices = new Map();
  const semanticParameterDefaults = new Map();
  let fallbackMouthPrimaryIndex = -1;
  let fallbackMouthSecondaryIndex = -1;
  let angleXIndex = -1;
  let angleYIndex = -1;
  let angleZIndex = -1;
  let lastHaloEnergy = -1;
  let lastHaloUpdateAt = -Infinity;
  let moveMode = false;
  let dragging = false;

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function knownActionIds(value) {
    return Array.isArray(value) ? value.filter((actionId) => actionsById.has(actionId)) : [];
  }

  function normalizedActivityRule(value) {
    if (Array.isArray(value)) return { add: knownActionIds(value), suppress: [] };
    if (!value || typeof value !== "object") return { add: [], suppress: [] };
    return {
      add: knownActionIds(value.add),
      suppress: knownActionIds(value.suppress),
    };
  }

  function mergeActivityActions(parent, child) {
    const merged = {};
    for (const state of Object.keys(colors)) {
      const parentRule = normalizedActivityRule(parent?.[state]);
      const childHasState = Object.prototype.hasOwnProperty.call(child || {}, state);
      if (!childHasState) {
        merged[state] = parentRule;
        continue;
      }
      const childRule = child[state];
      if (Array.isArray(childRule)) {
        merged[state] = { add: knownActionIds(childRule), suppress: [] };
        continue;
      }
      const normalizedChild = normalizedActivityRule(childRule);
      merged[state] = {
        add: Object.prototype.hasOwnProperty.call(childRule || {}, "add")
          ? normalizedChild.add
          : parentRule.add,
        suppress: Object.prototype.hasOwnProperty.call(childRule || {}, "suppress")
          ? normalizedChild.suppress
          : parentRule.suppress,
      };
    }
    return merged;
  }

  function viewportDimension(value, fallback) {
    const dimension = Number(value);
    return Number.isFinite(dimension) && dimension > 0 ? Math.max(1, Math.floor(dimension)) : fallback;
  }

  function resolveViewport(payload) {
    return {
      width: viewportDimension(payload?.width ?? window.innerWidth, FALLBACK_WIDTH),
      height: viewportDimension(payload?.height ?? window.innerHeight, FALLBACK_HEIGHT),
    };
  }

  function resizeViewport(payload) {
    const { width, height } = resolveViewport(payload);
    const changed = width !== viewportWidth || height !== viewportHeight;
    viewportWidth = width;
    viewportHeight = height;
    if (!changed) return;
    if (app?.renderer && typeof app.renderer.resize === "function") {
      app.renderer.resize(width, height);
    }
    if (model) fitModel();
  }

  function operationBlend(operation) {
    const blend = typeof operation?.blend === "string" ? operation.blend.toLowerCase() : "add";
    if (blend === "overwrite") return OPERATION_SET;
    if (blend === "multiply") return OPERATION_MULTIPLY;
    return OPERATION_ADD;
  }

  function compileOperation(operation) {
    if (!operation || typeof operation.parameter_id !== "string") return null;
    const value = Number(operation.value);
    if (!Number.isFinite(value)) return null;
    return {
      parameterId: operation.parameter_id,
      parameterIndex: -1,
      value,
      blend: operationBlend(operation),
    };
  }

  function resolveParameterIndex(parameterId) {
    if (parameterIndices.has(parameterId)) return parameterIndices.get(parameterId);
    const index = coreModel.getParameterIndex(parameterId);
    parameterIndices.set(parameterId, index);
    return index;
  }

  function resolveModelBindings() {
    for (const fixed of fixedParameters) fixed.parameterIndex = resolveParameterIndex(fixed.parameterId);
    for (const fixed of fixedParts) fixed.partIndex = coreModel.getPartIndex(fixed.partId);
    for (const operation of safeDefaults) operation.parameterIndex = resolveParameterIndex(operation.parameterId);
    for (const action of actionsInReplayOrder) {
      for (const operation of action.operations) {
        operation.parameterIndex = resolveParameterIndex(operation.parameterId);
        const defaultValue = coreModel.getParameterDefaultValue(operation.parameterIndex);
        if (Number.isFinite(defaultValue)) {
          semanticParameterDefaults.set(operation.parameterIndex, defaultValue);
        }
      }
    }
    for (const target of speechMotionTargets) target.parameterIndex = resolveParameterIndex(target.parameterId);
    if (mouthMotion) {
      mouthMotion.primaryParameterIndex = resolveParameterIndex(mouthMotion.primaryParameterId);
      mouthMotion.secondaryParameterIndex = mouthMotion.secondaryParameterId
        ? resolveParameterIndex(mouthMotion.secondaryParameterId)
        : -1;
      mouthMotion.jawParameterIndex = mouthMotion.jawParameterId
        ? resolveParameterIndex(mouthMotion.jawParameterId)
        : -1;
    } else {
      fallbackMouthPrimaryIndex = resolveParameterIndex("ParamMouthOpenY");
      fallbackMouthSecondaryIndex = resolveParameterIndex("ParamMouthOpenY2");
    }
    if (eyelidMotion) {
      eyelidMotion.leftParameterIndex = resolveParameterIndex(eyelidMotion.leftParameterId);
      eyelidMotion.rightParameterIndex = resolveParameterIndex(eyelidMotion.rightParameterId);
    }
    angleXIndex = resolveParameterIndex("ParamAngleX");
    angleYIndex = resolveParameterIndex("ParamAngleY");
    angleZIndex = resolveParameterIndex("ParamAngleZ");
  }

  function rebuildComposedOperations() {
    const overwrite = [];
    const add = [];
    const multiply = [];
    const append = (operation) => {
      if (operation.blend === OPERATION_SET) overwrite.push(operation);
      else if (operation.blend === OPERATION_MULTIPLY) multiply.push(operation);
      else add.push(operation);
    };
    for (const operation of safeDefaults) append(operation);
    for (const action of actionsInReplayOrder) {
      const controllerActive = activeActionIds.has(action.id) && !suppressedActionIds.has(action.id);
      if (!controllerActive && !activityActionIds.has(action.id)) continue;
      for (const operation of action.operations) append(operation);
    }
    composedOperations = overwrite.concat(add, multiply);
  }

  function applyActivityRule() {
    const rule = normalizedActivityRule(activityActions[activity]);
    activityActionIds = new Set(rule.add);
    suppressedActionIds = new Set(rule.suppress);
    rebuildComposedOperations();
  }

  function setActivity(nextActivity, ttlMs = 0) {
    if (!Object.prototype.hasOwnProperty.call(colors, nextActivity)) return;
    const requestedTtl = Number(ttlMs);
    const boundedTtl = Number.isFinite(requestedTtl) && requestedTtl > 0
      ? clamp(requestedTtl, 500, 30000)
      : 0;
    if (activity === nextActivity) {
      if (nextActivity === "idle") activityExpiresAt = 0;
      else if (boundedTtl > 0) activityExpiresAt = performance.now() + boundedTtl;
      return;
    }
    activity = nextActivity;
    applyActivityRule();
    activityExpiresAt = activity === "idle" || boundedTtl === 0 ? 0 : performance.now() + boundedTtl;
    document.documentElement.style.setProperty("--accent", colors[activity]);
  }

  function expireActivityIfNeeded(nowMilliseconds) {
    if (activity !== "idle" && activityExpiresAt > 0 && nowMilliseconds >= activityExpiresAt) {
      setActivity("idle");
    }
  }

  function setHaloVisible(enabled) {
    document.documentElement.style.setProperty("--halo-display", enabled ? "block" : "none");
  }

  function applyAudioEvent(event) {
    if (!event || typeof event !== "object") return;
    if (event.type === "activity") {
      setActivity(event.state, event.ttl_ms);
    } else if (event.type === "state") {
      speaking = event.state === "speaking";
      if (!speaking) amplitude *= 0.55;
    } else if (event.type === "audio") {
      amplitude = clamp(Number(event.amplitude) || 0, 0, 1);
      if (Array.isArray(event.bands) && event.bands.length) {
        let sum = 0;
        for (let index = 0; index < event.bands.length; index += 1) {
          sum += clamp(Number(event.bands[index]) || 0, 0, 1);
        }
        bandEnergy = sum / event.bands.length;
      } else {
        bandEnergy = 0;
      }
    }
  }

  function applyOperation(operation) {
    if (operation.blend === OPERATION_ADD) {
      coreModel.addParameterValueByIndex(operation.parameterIndex, operation.value);
    } else if (operation.blend === OPERATION_MULTIPLY) {
      coreModel.multiplyParameterValueByIndex(operation.parameterIndex, operation.value);
    } else {
      coreModel.setParameterValueByIndex(operation.parameterIndex, operation.value);
    }
  }

  function setParameter(index, value) {
    if (Number.isFinite(value)) coreModel.setParameterValueByIndex(index, value);
  }

  function addParameter(index, value) {
    if (Number.isFinite(value) && Math.abs(value) >= 0.000001) {
      coreModel.addParameterValueByIndex(index, value);
    }
  }

  function applyFixedControls() {
    for (const fixed of fixedParameters) setParameter(fixed.parameterIndex, fixed.value);
    for (const fixed of fixedParts) {
      if (typeof coreModel.setPartOpacityByIndex === "function") {
        coreModel.setPartOpacityByIndex(fixed.partIndex, fixed.opacity);
      }
    }
  }

  function applyAvatarState(state) {
    if (!state || state.avatar_id !== config.avatar_id || !Array.isArray(state.actions)) return;
    avatarStateApplied = true;
    activeActionIds = new Set(state.actions.filter((actionId) => actionsById.has(actionId)));
    rebuildComposedOperations();
    console.info("Live2D avatar state applied", {
      revision: Number.isInteger(state.revision) ? state.revision : null,
      actions: actionsInReplayOrder.filter((action) => activeActionIds.has(action.id)).map((action) => action.id),
    });
  }

  function applyProfileCuration(curation) {
    if (!curation || curation.schema !== "codex-ai-presence/profile-curation/v0.1") return;
    if (!avatarStateApplied && Object.prototype.hasOwnProperty.call(curation, "initial_actions")) {
      activeActionIds = new Set(knownActionIds(curation.initial_actions));
    }
    const childActivity = curation.activity_actions && typeof curation.activity_actions === "object"
      ? curation.activity_actions
      : {};
    activityActions = mergeActivityActions(baseActivityActions, childActivity);
    applyActivityRule();
    console.info("Live2D profile curation applied", {
      profile_id: curation.profile_id || null,
      route_key: curation.route_key || null,
    });
  }

  function applyEffectiveSnapshot(snapshot) {
    if (!snapshot || snapshot.schema !== "presence/renderer-snapshot/v0.2") return;
    if (!snapshot.semantic || !Array.isArray(snapshot.semantic.effective_actions)) return;
    const avatarId = String(snapshot.avatar_ref || "").split("@", 1)[0];
    if (avatarId && avatarId !== config.avatar_id) {
      throw new Error(`Resolved avatar ${avatarId} does not match renderer ${config.avatar_id}`);
    }
    avatarStateApplied = true;
    activeActionIds = new Set(knownActionIds(snapshot.semantic.effective_actions));
    // v0.2 already includes the activity overlay. Never re-resolve the model's
    // legacy activity table in JavaScript.
    activityActions = {};
    activity = typeof snapshot.semantic.activity === "string"
      ? snapshot.semantic.activity
      : "idle";
    rebuildComposedOperations();
  }

  function fitModel() {
    const bounds = model.getLocalBounds();
    if (!(bounds.width > 0 && bounds.height > 0)) throw new Error("Avatar has no drawable bounds");
    const layoutScale = Math.min(viewportWidth / FALLBACK_WIDTH, viewportHeight / FALLBACK_HEIGHT);
    const scale = avatarScale * Math.min(
      (viewportWidth * 0.9) / bounds.width,
      (viewportHeight * 0.93) / bounds.height,
    );
    model.scale.set(scale);
    baseX = viewportWidth / 2 - (bounds.x + bounds.width / 2) * scale;
    baseY = viewportHeight - bottomInset * layoutScale - (bounds.y + bounds.height) * scale;
    model.position.set(baseX, baseY);
  }

  function renderAvatar() {
    const nowMilliseconds = performance.now();
    expireActivityIfNeeded(nowMilliseconds);
    const now = nowMilliseconds / 1000;
    amplitude *= speaking ? 0.985 : 0.93;
    bandEnergy *= speaking ? 0.99 : 0.95;
    const voiceInput = speaking ? clamp(amplitude * 0.68 + bandEnergy * 0.32, 0, 1) : 0;
    speechEnergy += (voiceInput - speechEnergy) * (speaking ? 0.11 : 0.18);
    if (eyelidMotion) {
      eyelidEnergy += (voiceInput - eyelidEnergy) * (speaking ? eyelidMotion.attack : eyelidMotion.release);
    }
    if (mouthMotion) {
      mouthEnergy += (voiceInput - mouthEnergy) * (speaking ? mouthMotion.attack : mouthMotion.release);
    }
    const breath = Math.sin(now * 1.15);
    const headTilt = Math.sin(now * 0.72) * 0.75 + speechEnergy * Math.sin(now * 2.1) * 0.3;

    if (mouthMotion) {
      const mouthOpen = speaking ? clamp(mouthMotion.baseOpen + mouthEnergy * mouthMotion.mouthGain, 0, 1) : 0;
      const jawOpen = speaking ? clamp(mouthEnergy * mouthMotion.jawGain, 0, 1) : 0;
      setParameter(mouthMotion.primaryParameterIndex, mouthOpen);
      if (mouthMotion.secondaryParameterIndex >= 0) {
        setParameter(mouthMotion.secondaryParameterIndex, mouthOpen * mouthMotion.secondaryGain);
      }
      if (mouthMotion.jawParameterIndex >= 0) setParameter(mouthMotion.jawParameterIndex, jawOpen);
    } else {
      const mouth = speaking ? clamp(0.06 + amplitude * 1.15 + bandEnergy * 0.25, 0, 1) : 0;
      setParameter(fallbackMouthPrimaryIndex, mouth);
      setParameter(fallbackMouthSecondaryIndex, mouth * 0.82);
    }
    for (const target of speechMotionTargets) {
      const phase = now * target.angularFrequency + target.phase;
      addParameter(target.parameterIndex, Math.sin(phase) * (target.idleGain + speechEnergy * target.speechGain));
    }
    // Expression files use relative Add operations. Reset each semantic control
    // to its Cubism default before replaying the selected action set, otherwise
    // an action removed from a routed session can leave a clothing/accessory
    // parameter latched from its previous frame.
    for (const [parameterIndex, defaultValue] of semanticParameterDefaults) {
      setParameter(parameterIndex, defaultValue);
    }
    for (const operation of composedOperations) applyOperation(operation);
    applyFixedControls();
    if (eyelidMotion) {
      const idlePhase = now * eyelidMotion.idleFrequency * Math.PI * 2;
      const idleOpenness = eyelidMotion.idleOpenMin
        + (eyelidMotion.idleOpenMax - eyelidMotion.idleOpenMin) * (0.5 + Math.sin(idlePhase) * 0.5);
      const dynamicWake = clamp(eyelidEnergy * eyelidMotion.wakeGain, 0, 1);
      const wake = speaking ? Math.max(eyelidMotion.talkingWakeFloor, dynamicWake) : 0;
      const openness = clamp(
        idleOpenness + (eyelidMotion.speechOpen - idleOpenness) * wake,
        eyelidMotion.idleOpenMin,
        eyelidMotion.speechOpen,
      );
      setParameter(eyelidMotion.leftParameterIndex, openness);
      setParameter(eyelidMotion.rightParameterIndex, openness);
    }
    addParameter(angleXIndex, Math.sin(now * 0.5) * 1.1);
    addParameter(angleYIndex, breath * 0.7);
    addParameter(angleZIndex, headTilt);
    if (haloEnabled && nowMilliseconds - lastHaloUpdateAt >= HALO_UPDATE_INTERVAL_MS) {
      const energy = clamp(0.14 + amplitude * 0.82 + bandEnergy * 0.2 + (activity === "idle" ? 0 : 0.08), 0.12, 1);
      if (lastHaloEnergy < 0 || Math.abs(energy - lastHaloEnergy) >= HALO_ENERGY_EPSILON) {
        lastHaloEnergy = energy;
        document.documentElement.style.setProperty("--energy", energy.toFixed(3));
      }
      lastHaloUpdateAt = nowMilliseconds;
    }
  }

  function enabledFromPayload(payload) {
    return typeof payload === "object" && payload !== null
      ? Boolean(payload.enabled)
      : Boolean(payload);
  }

  function setMoveMode(payload) {
    moveMode = enabledFromPayload(payload);
    if (!moveMode) {
      dragging = false;
      document.body.classList.remove("dragging");
    }
    document.body.classList.toggle("move-mode", moveMode);
  }

  function setResizeMode(enabled) {
    const active = Boolean(enabled);
    document.body.classList.toggle("resize-mode", active);
    if (active) document.body.classList.remove("dragging");
  }

  function attachOrbBridge() {
    if (!window.orbApi) return;
    window.orbApi.onAudioEvent(applyAudioEvent);
    window.orbApi.onProfileCuration?.(applyProfileCuration);
    window.orbApi.onAvatarState?.(applyAvatarState);
    window.orbApi.onMoveMode(setMoveMode);
    window.orbApi.onResizeMode?.(setResizeMode);
    window.orbApi.onWindowResize?.(resizeViewport);
    window.addEventListener("resize", () => resizeViewport());
  }

  function attachPresenceBridge() {
    if (!window.presenceRenderer) return;
    window.presenceRenderer.onSnapshot(applyEffectiveSnapshot);
    window.presenceRenderer.onEvent(applyAudioEvent);
  }

  async function start() {
    if (!window.PIXI?.live2d?.Live2DModel) throw new Error("Local Live2D renderer did not load");
    if (!config.model || typeof config.model.path !== "string") throw new Error("Avatar capabilities have no model path");
    PIXI.live2d.CubismConfig.supportMoreMaskDivisions = true;
    setHaloVisible(haloEnabled);
    resizeViewport();
    app = new PIXI.Application({
      view: canvas,
      width: viewportWidth,
      height: viewportHeight,
      transparent: true,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      autoStart: false,
      powerPreference: "high-performance",
      resolution: RENDER_RESOLUTION,
    });
    model = await PIXI.live2d.Live2DModel.from(config.model.path, {
      autoInteract: false,
      autoFocus: false,
      autoUpdate: false,
    });
    model.interactive = false;
    model.interactiveChildren = false;
    app.stage.interactiveChildren = false;
    app.stage.addChild(model);
    fitModel();
    const internalModel = model.internalModel;
    if (!internalModel || typeof internalModel.on !== "function") {
      throw new Error("Live2D model does not expose the Cubism update lifecycle");
    }
    coreModel = internalModel.coreModel;
    if (!coreModel || typeof coreModel.getParameterIndex !== "function") {
      throw new Error("Live2D model does not expose indexed Cubism parameters");
    }
    resolveModelBindings();
    rebuildComposedOperations();
    internalModel.on("beforeModelUpdate", renderAvatar);
    const updatePriority = Number(PIXI.UPDATE_PRIORITY?.HIGH) || 25;
    app.ticker.add(() => model.update(app.ticker.elapsedMS), null, updatePriority);
    app.start();
    applyActivityRule();
    console.info("Live2D avatar state renderer ready", config.avatar_id);
    window.presenceRenderer?.ready();
  }

  attachOrbBridge();
  attachPresenceBridge();
  start().catch((error) => {
    console.error("Live2D avatar load failed", error);
    window.presenceRenderer?.failed(String(error?.message || error));
  });
})();

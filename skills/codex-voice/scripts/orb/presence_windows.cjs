"use strict";

const PROFILE_SCHEMA = "codex-ai-presence/profiles/v0.1";
const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;
const ACTION_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const ACTIVITY_STATES = new Set(["idle", "thinking", "tool", "skill", "cli", "waiting", "error"]);
const MAX_CURATION_ACTIONS = 128;

function validId(value, fallback) {
  return typeof value === "string" && ID_PATTERN.test(value) ? value : fallback;
}

function curationActions(value) {
  if (!Array.isArray(value) || value.length > MAX_CURATION_ACTIONS) return null;
  if (!value.every((actionId) => typeof actionId === "string" && ACTION_ID_PATTERN.test(actionId))) return null;
  if (new Set(value).size !== value.length) return null;
  return [...value];
}

function profileCuration(profile) {
  if (!profile || typeof profile !== "object" || Array.isArray(profile)) return null;
  const raw = profile.curation;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (Object.keys(raw).some((key) => !["initial_actions", "activity_actions"].includes(key))) return null;
  const normalized = {};
  if (Object.prototype.hasOwnProperty.call(raw, "initial_actions")) {
    const actions = curationActions(raw.initial_actions);
    if (!actions) return null;
    normalized.initial_actions = actions;
  }
  if (Object.prototype.hasOwnProperty.call(raw, "activity_actions")) {
    if (!raw.activity_actions || typeof raw.activity_actions !== "object" || Array.isArray(raw.activity_actions)) {
      return null;
    }
    const activityActions = {};
    for (const [state, rawRule] of Object.entries(raw.activity_actions)) {
      if (!ACTIVITY_STATES.has(state) || !rawRule || typeof rawRule !== "object" || Array.isArray(rawRule)) {
        return null;
      }
      if (Object.keys(rawRule).some((key) => !["add", "suppress"].includes(key))) return null;
      const rule = {};
      for (const field of ["add", "suppress"]) {
        if (!Object.prototype.hasOwnProperty.call(rawRule, field)) continue;
        const actions = curationActions(rawRule[field]);
        if (!actions) return null;
        rule[field] = actions;
      }
      activityActions[state] = rule;
    }
    normalized.activity_actions = activityActions;
  }
  return normalized;
}

function descriptorForProfile(sessionId, profileId, profile, fallbackAvatarId) {
  const descriptor = {
    key: `session:${sessionId || "unscoped"}|profile:${profileId}`,
    sessionId: sessionId || null,
    profileId,
    avatarId: validId(profile?.avatar_id, fallbackAvatarId),
  };
  const curation = profileCuration(profile);
  if (curation) descriptor.curation = curation;
  return descriptor;
}

function windowDescriptors(document, selectedAvatarId = "builtin", eligibleSessionIds = null) {
  const fallbackAvatarId = validId(selectedAvatarId, "builtin");
  if (!document || typeof document !== "object" || document.schema !== PROFILE_SCHEMA) {
    return [{
      key: "session:unscoped|profile:default",
      sessionId: null,
      profileId: "default",
      avatarId: fallbackAvatarId,
    }];
  }
  const profiles = document.profiles && typeof document.profiles === "object" ? document.profiles : {};
  const projectProfileId = validId(document.project_profile_id, "default");
  if (!Object.prototype.hasOwnProperty.call(profiles, projectProfileId)) {
    return [{
      key: "session:unscoped|profile:default",
      sessionId: null,
      profileId: "default",
      avatarId: fallbackAvatarId,
    }];
  }
  const projectProfile = profiles[projectProfileId] && typeof profiles[projectProfileId] === "object"
    ? profiles[projectProfileId]
    : {};
  const sessions = document.sessions && typeof document.sessions === "object" ? document.sessions : {};
  const configuredSessionIds = Object.keys(sessions).filter((sessionId) => sessionId.trim()).sort();
  const eligible = eligibleSessionIds instanceof Set ? eligibleSessionIds : null;
  const descriptors = [];
  for (const sessionId of configuredSessionIds) {
    if (eligible && !eligible.has(sessionId)) continue;
    const binding = sessions[sessionId];
    const candidateProfileId = typeof binding === "string" ? binding : binding?.profile_id;
    const profileId = validId(candidateProfileId, projectProfileId);
    if (!Object.prototype.hasOwnProperty.call(profiles, profileId)) continue;
    const profile = profiles[profileId] && typeof profiles[profileId] === "object"
      ? profiles[profileId]
      : projectProfile;
    descriptors.push(descriptorForProfile(sessionId, profileId, profile, fallbackAvatarId));
  }
  if (descriptors.length) return descriptors;
  // Once a project has explicit session bindings, an enabled-session filter
  // that rejects every binding must produce no window. Falling back to the
  // project profile here would resurrect stale or cross-project renderers.
  if (eligible && configuredSessionIds.length) return [];
  if (eligible && eligible.size === 1) {
    const [sessionId] = eligible;
    return [descriptorForProfile(sessionId, projectProfileId, projectProfile, fallbackAvatarId)];
  }
  if (eligible && eligible.size !== 1) return [];
  return [descriptorForProfile(null, projectProfileId, projectProfile, fallbackAvatarId)];
}

function routeWindowKeys(descriptors, event, foregroundKey = null) {
  if (!Array.isArray(descriptors) || descriptors.length === 0) return [];
  const routeKey = typeof event?.route_key === "string" ? event.route_key : null;
  if (routeKey) {
    const route = descriptors.find((descriptor) => descriptor.key === routeKey);
    return route ? [route.key] : [];
  }
  const sessionId = typeof event?.session_id === "string" ? event.session_id : null;
  const profileId = typeof event?.profile_id === "string" ? event.profile_id : null;
  if (sessionId) {
    const exact = descriptors.filter((descriptor) => (
      descriptor.sessionId === sessionId && (!profileId || descriptor.profileId === profileId)
    ));
    if (exact.length) return exact.map((descriptor) => descriptor.key);
    // A scoped packet must never fall through to another session's avatar.
    return [];
  }
  if (["audio", "state"].includes(event?.type) && foregroundKey) return [foregroundKey];
  if (["audio", "state"].includes(event?.type)) {
    return descriptors.length === 1 ? [descriptors[0].key] : [];
  }
  if (descriptors.length === 1) return [descriptors[0].key];
  if (event?.type === "activity") return descriptors.map((descriptor) => descriptor.key);
  return foregroundKey ? [foregroundKey] : [descriptors[0].key];
}

function avatarStateForWindow(descriptor, avatarId, routedStates, projectState = null) {
  if (!descriptor || typeof descriptor.key !== "string") return null;
  const routed = routedStates instanceof Map
    ? routedStates.get(descriptor.key)
    : routedStates?.[descriptor.key];
  if (routed?.avatar_id === avatarId && routed?.route_key === descriptor.key) return routed;
  return projectState?.avatar_id === avatarId ? projectState : null;
}

module.exports = { avatarStateForWindow, profileCuration, routeWindowKeys, windowDescriptors };

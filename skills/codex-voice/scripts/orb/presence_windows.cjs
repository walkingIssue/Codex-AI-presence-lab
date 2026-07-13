"use strict";

const PROFILE_SCHEMA = "codex-ai-presence/profiles/v0.1";
const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;

function validId(value, fallback) {
  return typeof value === "string" && ID_PATTERN.test(value) ? value : fallback;
}

function windowDescriptors(document, selectedAvatarId = "builtin") {
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
  const descriptors = [];
  for (const sessionId of Object.keys(sessions).sort()) {
    if (!sessionId.trim()) continue;
    const binding = sessions[sessionId];
    const candidateProfileId = typeof binding === "string" ? binding : binding?.profile_id;
    const profileId = validId(candidateProfileId, projectProfileId);
    if (!Object.prototype.hasOwnProperty.call(profiles, profileId)) continue;
    const profile = profiles[profileId] && typeof profiles[profileId] === "object"
      ? profiles[profileId]
      : projectProfile;
    descriptors.push({
      key: `session:${sessionId}|profile:${profileId}`,
      sessionId,
      profileId,
      avatarId: validId(profile.avatar_id, fallbackAvatarId),
    });
  }
  if (descriptors.length) return descriptors;
  return [{
    key: `session:unscoped|profile:${projectProfileId}`,
    sessionId: null,
    profileId: projectProfileId,
    avatarId: validId(projectProfile.avatar_id, fallbackAvatarId),
  }];
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

module.exports = { avatarStateForWindow, routeWindowKeys, windowDescriptors };

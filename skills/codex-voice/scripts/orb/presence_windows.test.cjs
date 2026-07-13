"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { routeWindowKeys, windowDescriptors } = require("./presence_windows.cjs");

test("materializes one avatar window per bound session", () => {
  const descriptors = windowDescriptors({
    schema: "codex-ai-presence/profiles/v0.1",
    project_profile_id: "sol",
    profiles: {
      sol: { avatar_id: "builtin" },
      luna: { avatar_id: "higan-live2d" },
    },
    sessions: {
      "session-sol": { profile_id: "sol" },
      "session-luna": { profile_id: "luna" },
    },
  });
  assert.deepEqual(descriptors.map(({ sessionId, profileId, avatarId }) => ({ sessionId, profileId, avatarId })), [
    { sessionId: "session-luna", profileId: "luna", avatarId: "higan-live2d" },
    { sessionId: "session-sol", profileId: "sol", avatarId: "builtin" },
  ]);
});

test("routes session activity and unscoped speech audio independently", () => {
  const descriptors = [
    { key: "session:a|profile:sol", sessionId: "a", profileId: "sol", avatarId: "builtin" },
    { key: "session:b|profile:luna", sessionId: "b", profileId: "luna", avatarId: "higan-live2d" },
  ];
  assert.deepEqual(
    routeWindowKeys(descriptors, { type: "activity", session_id: "b", profile_id: "luna" }),
    ["session:b|profile:luna"],
  );
  assert.deepEqual(
    routeWindowKeys(descriptors, { type: "audio" }, "session:b|profile:luna"),
    ["session:b|profile:luna"],
  );
  assert.deepEqual(
    routeWindowKeys(descriptors, { type: "activity", session_id: "unknown", profile_id: "sol" }),
    [],
  );
  assert.deepEqual(
    routeWindowKeys(descriptors, { type: "state", state: "speaking" }),
    [],
  );
  assert.deepEqual(
    routeWindowKeys(descriptors, { type: "voice-output", route_key: "session:a|profile:sol" }),
    ["session:a|profile:sol"],
  );
});

test("invalid profile references fail closed to the legacy renderer", () => {
  assert.deepEqual(
    windowDescriptors({
      schema: "codex-ai-presence/profiles/v0.1",
      project_profile_id: "missing",
      profiles: { sol: { avatar_id: "builtin" } },
      sessions: { a: { profile_id: "also-missing" } },
    }, "higan-live2d"),
    [{
      key: "session:unscoped|profile:default",
      sessionId: null,
      profileId: "default",
      avatarId: "higan-live2d",
    }],
  );
});

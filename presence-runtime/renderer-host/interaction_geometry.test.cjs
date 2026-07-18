"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { interactionBounds } = require("./interaction_geometry.cjs");

test("moves without changing binding geometry size", () => {
  assert.deepEqual(
    interactionBounds(
      { x: 10, y: 20, width: 400, height: 600 },
      { x: 100, y: 200 },
      { x: 130, y: 180 },
      "move",
    ),
    { x: 40, y: 0, width: 400, height: 600 },
  );
});

test("resizes from the lower-right with a safe minimum", () => {
  assert.deepEqual(
    interactionBounds(
      { x: 10, y: 20, width: 400, height: 600 },
      { x: 100, y: 200 },
      { x: -1000, y: -1000 },
      "resize",
    ),
    { x: 10, y: 20, width: 160, height: 160 },
  );
});

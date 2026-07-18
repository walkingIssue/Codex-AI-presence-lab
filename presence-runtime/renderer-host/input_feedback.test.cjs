"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { InputFeedbackRegistry, acceptInputFeedbackEvent } = require("./input_feedback.cjs");

function event(bindingId, captureId, state) {
  return { binding_id: bindingId, capture_id: captureId, state };
}

test("tracks recording, inference, delivery, and terminal settling", () => {
  const feedback = new InputFeedbackRegistry();

  assert.equal(feedback.update(event("binding-a", "capture-a", "recording")).view.phase, "recording");
  assert.equal(feedback.update(event("binding-a", "capture-a", "transcribing")).view.phase, "transcribing");
  assert.equal(feedback.update(event("binding-a", "capture-a", "ready")).view.phase, "ready");
  const delivered = feedback.update(event("binding-a", "capture-a", "delivered")).view;
  assert.equal(delivered.phase, "delivered");
  assert.equal(feedback.settle("binding-a", delivered.flash_token).phase, "idle");
});

test("a stale completion cannot replace a newer recording", () => {
  const feedback = new InputFeedbackRegistry();
  feedback.update(event("binding-a", "old", "recording"));
  feedback.update(event("binding-a", "old", "transcribing"));
  feedback.update(event("binding-a", "new", "recording"));

  assert.equal(feedback.update(event("binding-a", "old", "ready")).view.capture_id, "new");
  const delivered = feedback.update(event("binding-a", "old", "delivered")).view;
  assert.equal(delivered.phase, "recording");
  assert.equal(delivered.capture_id, "new");
});

test("a terminal capture cannot be resurrected by a late inference result", () => {
  const feedback = new InputFeedbackRegistry();
  feedback.update(event("binding-a", "capture-a", "recording"));
  feedback.update(event("binding-a", "capture-a", "transcribing"));
  const delivered = feedback.update(event("binding-a", "capture-a", "delivered"));
  const late = feedback.update(event("binding-a", "capture-a", "ready"));

  assert.equal(delivered.view.phase, "delivered");
  assert.equal(late.changed, false);
  assert.equal(late.view.phase, "delivered");
});

test("bindings maintain independent input feedback", () => {
  const feedback = new InputFeedbackRegistry();
  feedback.update(event("binding-a", "capture-a", "recording"));
  feedback.update(event("binding-b", "capture-b", "recording"));
  feedback.update(event("binding-a", "capture-a", "transcribing"));

  assert.equal(feedback.view("binding-a").phase, "transcribing");
  assert.equal(feedback.view("binding-b").phase, "recording");
});

test("rejects malformed input feedback events", () => {
  assert.throws(
    () => acceptInputFeedbackEvent(event("binding-a", "capture-a", "guessing")),
    /unsupported/,
  );
  assert.throws(
    () => acceptInputFeedbackEvent({ binding_id: "binding-a", state: "recording" }),
    /capture_id/,
  );
});

test("host stylesheet defines every visible input phase", () => {
  const css = fs.readFileSync(path.join(__dirname, "input_feedback.css"), "utf8");
  for (const phase of [
    "targeted",
    "move",
    "resize",
    "recording",
    "transcribing",
    "ready",
    "delivered",
    "failed",
  ]) {
    assert.match(css, new RegExp(`data-presence-feedback=\\"${phase}\\"`));
  }
});

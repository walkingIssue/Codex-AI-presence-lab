"use strict";

const ACTIVE_PHASES = new Set(["recording", "transcribing", "ready"]);
const TERMINAL_PHASES = new Set(["delivered", "failed", "cancelled"]);
const PHASE_PRIORITY = Object.freeze({
  ready: 1,
  transcribing: 2,
  recording: 3,
});

function acceptInputFeedbackEvent(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("input feedback event must be an object");
  }
  const bindingId = input.binding_id;
  const captureId = input.capture_id;
  const state = input.state;
  if (typeof bindingId !== "string" || !bindingId) {
    throw new TypeError("input feedback binding_id is required");
  }
  if (typeof captureId !== "string" || !captureId) {
    throw new TypeError("input feedback capture_id is required");
  }
  if (!ACTIVE_PHASES.has(state) && !TERMINAL_PHASES.has(state)) {
    throw new TypeError("input feedback state is unsupported");
  }
  return Object.freeze({ binding_id: bindingId, capture_id: captureId, state });
}

class InputFeedbackRegistry {
  constructor() {
    this.bindings = new Map();
    this.order = 0;
  }

  _record(bindingId) {
    let record = this.bindings.get(bindingId);
    if (!record) {
      record = { jobs: new Map(), flash: null, revision: 0 };
      this.bindings.set(bindingId, record);
    }
    return record;
  }

  view(bindingId) {
    const record = this.bindings.get(bindingId);
    if (!record) {
      return Object.freeze({ binding_id: bindingId, phase: "idle", revision: 0 });
    }
    const active = [...record.jobs.values()].sort((left, right) => {
      const priority = PHASE_PRIORITY[right.phase] - PHASE_PRIORITY[left.phase];
      return priority || right.order - left.order;
    })[0];
    if (active) {
      return Object.freeze({
        binding_id: bindingId,
        capture_id: active.captureId,
        phase: active.phase,
        revision: record.revision,
      });
    }
    if (record.flash) {
      return Object.freeze({
        binding_id: bindingId,
        capture_id: record.flash.captureId,
        phase: record.flash.phase,
        revision: record.revision,
        flash_token: record.flash.token,
      });
    }
    return Object.freeze({ binding_id: bindingId, phase: "idle", revision: record.revision });
  }

  update(input) {
    const event = acceptInputFeedbackEvent(input);
    const record = this._record(event.binding_id);
    const existing = record.jobs.get(event.capture_id);
    let changed = false;

    if (ACTIVE_PHASES.has(event.state)) {
      if (event.state === "recording" || existing) {
        record.jobs.set(event.capture_id, {
          captureId: event.capture_id,
          phase: event.state,
          order: ++this.order,
        });
        record.flash = null;
        changed = true;
      }
    } else if (existing) {
      record.jobs.delete(event.capture_id);
      changed = true;
      if (
        record.jobs.size === 0
        && (event.state === "delivered" || event.state === "failed")
      ) {
        record.flash = {
          captureId: event.capture_id,
          phase: event.state,
          token: ++this.order,
        };
      }
    }

    if (changed) record.revision += 1;
    return Object.freeze({ changed, view: this.view(event.binding_id) });
  }

  settle(bindingId, token) {
    const record = this.bindings.get(bindingId);
    if (!record?.flash || record.flash.token !== token) return null;
    record.flash = null;
    record.revision += 1;
    return this.view(bindingId);
  }

  clear(bindingId) {
    const record = this.bindings.get(bindingId);
    if (!record) return this.view(bindingId);
    record.jobs.clear();
    record.flash = null;
    record.revision += 1;
    return this.view(bindingId);
  }

  remove(bindingId) {
    return this.bindings.delete(bindingId);
  }
}

module.exports = { InputFeedbackRegistry, acceptInputFeedbackEvent };

"use strict";

class WindowRegistry {
  constructor() {
    this.records = new Map();
  }

  current(bindingId) {
    return this.records.get(bindingId) || null;
  }

  async swap(bindingId, createReplacement) {
    const previous = this.current(bindingId);
    const replacement = await createReplacement(previous);
    if (!replacement || replacement.bindingId !== bindingId || !replacement.ready) {
      throw new Error("replacement renderer did not acknowledge readiness");
    }
    this.records.set(bindingId, replacement);
    if (previous && previous !== replacement) previous.destroy();
    return replacement;
  }

  remove(bindingId) {
    const record = this.current(bindingId);
    if (!record) return false;
    this.records.delete(bindingId);
    record.destroy();
    return true;
  }

  closeAll() {
    for (const record of this.records.values()) record.destroy();
    this.records.clear();
  }
}

module.exports = { WindowRegistry };


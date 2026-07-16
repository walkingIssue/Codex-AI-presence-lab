# Presence Runtime v0.2

This is the canonical source package for the machine-local Presence Runtime.
It resolves project/session configuration, owns bindings and durable routing,
supervises the shared voice/renderer processes, and exposes the presence CLI.

The installable codex-voice skill receives a generated projection of this
directory. Do not create or edit a second implementation beneath skills/.
Runtime state and catalog assets belong under CODEX_HOME/presence/; none are
release inputs.

The normative architecture contract is
[ADR 0002](../docs/adr/0002-presence-runtime-v0.2.md).


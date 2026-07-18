"""Deterministic model-curated semantic slot composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .validation import (
    validate_model_pack,
    validate_semantic,
    validate_semantic_references,
)


@dataclass(frozen=True, slots=True)
class SemanticComposition:
    persistent_actions: tuple[str, ...]
    effective_actions: tuple[str, ...]
    activity_rules: Mapping[str, Mapping[str, tuple[str, ...]]]
    provenance: Mapping[str, str]


class _ActionSet:
    def __init__(self, model_pack: Mapping[str, Any]) -> None:
        self._slots = model_pack["semantic_slots"]
        self._actions = model_pack["actions"]
        self.selected: list[str] = []
        self.source: dict[str, str] = {}

    def clone(self) -> "_ActionSet":
        clone = object.__new__(_ActionSet)
        clone._slots = self._slots
        clone._actions = self._actions
        clone.selected = list(self.selected)
        clone.source = dict(self.source)
        return clone

    def clear_slot(self, slot: str) -> None:
        self.selected = [
            action
            for action in self.selected
            if slot not in self._actions[action]["slots"]
        ]
        self.source = {action: self.source[action] for action in self.selected}

    def add(self, action: str, source: str) -> None:
        claimed_slots = self._actions[action]["slots"]
        conflicts: set[str] = set()
        for slot in claimed_slots:
            if not self._slots[slot]["exclusive"]:
                continue
            conflicts.update(
                selected
                for selected in self.selected
                if slot in self._actions[selected]["slots"]
            )
        if action in self.selected:
            conflicts.add(action)
        if conflicts:
            self.selected = [item for item in self.selected if item not in conflicts]
            for item in conflicts:
                self.source.pop(item, None)
        self.selected.append(action)
        self.source[action] = source


class SemanticComposer:
    """Compose persistent selections and one recomputed activity overlay."""

    def __init__(self, model_pack: Mapping[str, Any]) -> None:
        self.model_pack = validate_model_pack(model_pack)

    def compose(
        self,
        layers: Iterable[tuple[str, Mapping[str, Any] | None]],
        *,
        activity: str | None = None,
    ) -> SemanticComposition:
        persistent = _ActionSet(self.model_pack)
        rules: dict[str, dict[str, tuple[str, ...]]] = {}
        provenance: dict[str, str] = {}

        for source, raw_semantic in layers:
            if raw_semantic is None:
                continue
            semantic = validate_semantic(raw_semantic, path=f"{source}.semantic")
            validate_semantic_references(
                semantic,
                self.model_pack,
                f"{source}.semantic",
            )
            for slot in semantic.get("clear_slots", ()):
                persistent.clear_slot(slot)
                provenance[f"semantic.slots.{slot}"] = source
            for slot, actions in semantic.get("slots", {}).items():
                # A slot-addressed list replaces only this inherited slot.
                # An explicit empty list is therefore an unambiguous clear.
                persistent.clear_slot(slot)
                provenance[f"semantic.slots.{slot}"] = source
                for action in actions:
                    persistent.add(action, source)
            for state, rule in semantic.get("activity", {}).items():
                if rule is None:
                    rules.pop(state, None)
                    provenance[f"semantic.activity.{state}"] = source
                    continue
                merged = dict(rules.get(state, {}))
                for field in ("add", "clear_slots"):
                    if field in rule:
                        merged[field] = tuple(rule[field])
                        provenance[f"semantic.activity.{state}.{field}"] = source
                rules[state] = merged

        for action, source in persistent.source.items():
            provenance[f"semantic.actions.{action}"] = source

        effective = persistent.clone()
        if activity is not None and activity in rules:
            rule = rules[activity]
            source = f"activity:{activity}"
            for slot in rule.get("clear_slots", ()):
                effective.clear_slot(slot)
            for action in rule.get("add", ()):
                effective.add(action, source)

        frozen_rules = {
            state: {
                field: tuple(values)
                for field, values in rule.items()
            }
            for state, rule in rules.items()
        }
        return SemanticComposition(
            persistent_actions=tuple(persistent.selected),
            effective_actions=tuple(effective.selected),
            activity_rules=frozen_rules,
            provenance=provenance,
        )


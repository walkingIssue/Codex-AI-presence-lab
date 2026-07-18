"""Intent-level control API executed inside the authoritative runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .controller import RuntimeController
from .errors import NotFoundError, ValidationError


class ControlAPI:
    def __init__(
        self,
        controller: RuntimeController,
        *,
        migrator: Any | None = None,
        on_shutdown: Callable[[], None] | None = None,
        available_providers: set[str] | None = None,
        running_provider: str | None = None,
        input_available: bool = False,
        on_policy_changed: Callable[[dict[str, Any]], None] | None = None,
        adapter_manager: Any | None = None,
    ) -> None:
        self.controller = controller
        self.store = controller.store
        self.catalog = controller.catalog
        self.migrator = migrator
        self.on_shutdown = on_shutdown
        self.available_providers = set(available_providers or ())
        self.running_provider = running_provider
        self.input_available = input_available
        self.on_policy_changed = on_policy_changed
        self.adapter_manager = adapter_manager

    def execute(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        handlers = {
            "runtime.status": self.runtime_status,
            "runtime.doctor": self.runtime_doctor,
            "runtime.set_policy": self.runtime_set_policy,
            "runtime.shutdown": self.runtime_shutdown,
            "project.register": self.project_register,
            "project.unregister": self.project_unregister,
            "project.status": self.project_status,
            "project.set_profile": self.project_set_profile,
            "project.clear_profile": self.project_clear_profile,
            "project.relocate": self.project_relocate,
            "session.list": self.session_list,
            "session.show": self.session_show,
            "session.set_profile": self.session_set_profile,
            "session.set": self.session_set,
            "session.clear": self.session_clear,
            "session.focus": self.session_focus,
            "catalog.list": self.catalog_list,
            "catalog.show": self.catalog_show,
            "catalog.import": self.catalog_import,
            "catalog.export": self.catalog_export,
            "catalog.remove": self.catalog_remove,
            "avatar.use": self.avatar_use,
            "preset.use": self.preset_use,
            "inspect.effective": self.inspect_effective,
            "migrate.status": self.migrate_status,
            "migrate.retry": self.migrate_retry,
            "migrate.rollback": self.migrate_rollback,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise ValidationError(f"unsupported presence operation: {operation!r}")
        return handler(dict(arguments))

    def runtime_status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return self.runtime_doctor({})

    def runtime_doctor(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding_id = arguments.get("binding_id")
        result = self.controller.doctor(binding_id)
        if self.adapter_manager is not None:
            result["project_adapters"] = [
                self.adapter_manager.status(project["project_instance_id"])
                for project in self.store.list_projects()
            ]
        return result

    def runtime_set_policy(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments) - {"provider", "microphone_permission"}
        if unknown:
            raise ValidationError(f"unknown runtime policy fields: {sorted(unknown)}")
        provider = arguments.get("provider")
        if provider is not None and self.available_providers and provider not in self.available_providers:
            raise ValidationError(
                f"provider {provider!r} is not installed; available providers: "
                f"{sorted(self.available_providers)}"
            )
        microphone_permission = arguments.get("microphone_permission")
        if microphone_permission is True and not self.input_available:
            raise ValidationError(
                "voice input is not installed; run `presence runtime install --with-input` first"
            )
        policy = self.store.set_runtime_policy(
            provider=arguments.get("provider"),
            microphone_permission=microphone_permission,
        )
        if self.on_policy_changed is not None:
            self.on_policy_changed(policy)
        return {
            **policy,
            "restart_required": bool(
                provider is not None
                and self.running_provider is not None
                and provider != self.running_provider
            ),
        }

    def runtime_shutdown(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        if self.on_shutdown is not None:
            self.on_shutdown()
        return {"shutdown_requested": True}

    def project_register(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._required(arguments, "project_root")
        project = self.store.register_project(root)
        binding = self.store.ensure_binding(project["project_instance_id"])
        migration = None
        if self.migrator is not None:
            migration = self.migrator.migrate_on_registration(
                project["project_instance_id"],
                Path(root),
            )
        effective = self.controller.ensure_effective(binding["binding_id"])
        adapter = (
            self.adapter_manager.start_project(project)
            if self.adapter_manager is not None
            else None
        )
        return {
            "project": project,
            "binding": binding,
            "effective": effective.to_document(),
            "migration": migration,
            "adapter": adapter,
        }

    def project_unregister(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id(arguments)
        project = self.store.project(project_id)
        if self.adapter_manager is not None:
            self.adapter_manager.stop_project(project_id)
        try:
            cancelled = self.store.unregister_project(
                project_id,
                force=bool(arguments.get("all_sources", False)),
            )
        except BaseException:
            if self.adapter_manager is not None:
                self.adapter_manager.start_project(project)
            raise
        if self.adapter_manager is not None:
            self.adapter_manager.cleanup_project_files(
                project["project_root"], project_id=project_id
            )
        self.controller.sync_binding_visibility()
        return {
            "project_instance_id": project_id,
            "unregistered": True,
            "cancelled_speech": cancelled,
        }

    def project_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id(arguments)
        result = {
            "project": self.store.project(project_id),
            "default": self.store.project_default(project_id),
            "bindings": self.store.list_bindings(project_id=project_id),
        }
        if self.adapter_manager is not None:
            result["adapter"] = self.adapter_manager.status(project_id)
        return result

    def project_set_profile(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = self._project_id(arguments)
        reference = self._required(arguments, "profile_ref")
        return [
            snapshot.to_document()
            for snapshot in self.controller.use_profile(reference, project_id=project_id)
        ]

    def project_clear_profile(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = self._project_id(arguments)
        return [
            snapshot.to_document()
            for snapshot in self.controller.clear_profile(project_id=project_id)
        ]

    def project_relocate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id(arguments)
        previous = self.store.project(project_id)
        if self.adapter_manager is not None:
            self.adapter_manager.stop_project(project_id)
        relocated = self.store.relocate_project(
            project_id,
            self._required(arguments, "new_root"),
        )
        if self.adapter_manager is not None:
            self.adapter_manager.cleanup_project_files(
                previous["project_root"], project_id=project_id
            )
            relocated["adapter"] = self.adapter_manager.start_project(relocated)
        return relocated

    def session_list(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = self._project_id(arguments)
        return [
            binding
            for binding in self.store.list_bindings(project_id=project_id)
            if binding["scope"] == "session"
        ]

    def session_show(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding_id = self._binding_id(arguments)
        snapshot = self.store.effective_snapshot(binding_id)
        return {
            "binding": self.store.binding(binding_id),
            "override": self.store.session_override(binding_id),
            "effective": snapshot.to_document() if snapshot else None,
            "geometry": self.store.geometry(binding_id),
        }

    def session_set_profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding_id = self._binding_id(arguments)
        reference = self._required(arguments, "profile_ref")
        return self.controller.use_profile(
            reference,
            binding_id=binding_id,
        )[0].to_document()

    def session_set(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding_id = self._binding_id(arguments)
        changes = arguments.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValidationError("session set requires a non-empty changes object")
        return self.controller.update_session(
            binding_id,
            changes,
        ).to_document()

    def session_clear(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding_id = self._binding_id(arguments)
        fields = arguments.get("fields")
        if fields is None:
            return self.controller.set_session_override(binding_id, None).to_document()
        if not isinstance(fields, list) or any(not isinstance(item, str) for item in fields):
            raise ValidationError("session clear fields must be a string list")
        return self.controller.update_session(
            binding_id,
            {},
            clear_fields=tuple(fields),
        ).to_document()

    def session_focus(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding_id = self._binding_id(arguments)
        self.store.focus_binding(binding_id)
        return self.store.attention()

    def catalog_list(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        kind = self._required(arguments, "kind")
        if kind == "avatar":
            return self.catalog.list_avatars()
        if kind == "profile":
            return self.catalog.list_profiles()
        if kind == "preset":
            return self.catalog.list_presets()
        raise ValidationError(f"unsupported catalog kind: {kind!r}")

    def catalog_show(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = self._required(arguments, "kind")
        reference = self._required(arguments, "reference")
        if kind == "avatar":
            document = self.catalog.get_avatar(reference)
            # Normal inspection exposes semantic ids and capabilities, not raw
            # renderer operations or source paths.
            return {
                "schema": document["schema"],
                "avatar_id": document["avatar_id"],
                "version": document["version"],
                "model_fingerprint": document["model_fingerprint"],
                "semantic_slots": document["semantic_slots"],
                "actions": {
                    action_id: {
                        key: value
                        for key, value in definition.items()
                        if key in {"label", "description", "slots"}
                    }
                    for action_id, definition in document["actions"].items()
                },
                "capabilities": document["capabilities"],
            }
        if kind == "profile":
            return self.catalog.get_profile(reference)
        if kind == "preset":
            return self.catalog.get_preset(reference)
        raise ValidationError(f"unsupported catalog kind: {kind!r}")

    def catalog_import(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(self._required(arguments, "source"))
        assets = arguments.get("assets")
        if assets is None:
            kind, reference = self.catalog.import_portable(source)
        else:
            document = json.loads(source.expanduser().resolve().read_text(encoding="utf-8"))
            reference = self.catalog.register_avatar(
                document,
                assets=Path(assets),
            )
            kind = "avatar"
        reconciled = self.controller.reconcile_catalog()
        return {
            "kind": kind,
            "reference": reference,
            "reconciled_bindings": [item.binding_id for item in reconciled],
        }

    def catalog_export(self, arguments: dict[str, Any]) -> dict[str, Any]:
        output = self.catalog.export(
            self._required(arguments, "kind"),
            self._required(arguments, "reference"),
            Path(self._required(arguments, "output")),
            force=bool(arguments.get("force", False)),
        )
        return {"output": str(output)}

    def catalog_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = self._required(arguments, "kind")
        reference = self._required(arguments, "reference")
        self.controller.remove_catalog_entry(
            kind,
            reference,
            force=bool(arguments.get("force", False)),
        )
        return {"kind": kind, "reference": reference, "removed": True}

    def avatar_use(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        project_id, binding_id = self._scope(arguments)
        snapshots = self.controller.use_avatar(
            self._required(arguments, "reference"),
            project_id=project_id,
            binding_id=binding_id,
            clear_preset=bool(arguments.get("clear_preset", False)),
        )
        return [item.to_document() for item in snapshots]

    def preset_use(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        project_id, binding_id = self._scope(arguments)
        snapshots = self.controller.use_preset(
            arguments.get("reference"),
            project_id=project_id,
            binding_id=binding_id,
        )
        return [item.to_document() for item in snapshots]

    def inspect_effective(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("binding_id") or arguments.get("session_id"):
            binding_id = self._binding_id(arguments)
        else:
            project_id = self._project_id(arguments)
            project_bindings = [
                item
                for item in self.store.list_bindings(project_id=project_id)
                if item["scope"] == "project"
            ]
            if not project_bindings:
                raise NotFoundError("project binding does not exist")
            binding_id = project_bindings[0]["binding_id"]
        snapshot = self.store.effective_snapshot(binding_id)
        if snapshot is None:
            snapshot = self.controller.ensure_effective(binding_id)
        return snapshot.to_document()

    def migrate_status(self, arguments: dict[str, Any]) -> Any:
        if self.migrator is None:
            return {"available": False}
        return self.migrator.status(self._project_id(arguments))

    def migrate_retry(self, arguments: dict[str, Any]) -> Any:
        if self.migrator is None:
            raise ValidationError("migration support is unavailable")
        project_id = self._project_id(arguments)
        project = self.store.project(project_id)
        return self.migrator.retry(project_id, Path(project["project_root"]))

    def migrate_rollback(self, arguments: dict[str, Any]) -> Any:
        if self.migrator is None:
            raise ValidationError("migration support is unavailable")
        return self.migrator.rollback(self._project_id(arguments))

    def _project_id(self, arguments: Mapping[str, Any]) -> str:
        project_id = arguments.get("project_id")
        if isinstance(project_id, str) and project_id:
            return project_id
        project_root = arguments.get("project_root")
        if isinstance(project_root, str) and project_root:
            return self.store.project_for_root(project_root)["project_instance_id"]
        raise ValidationError("project scope requires project_id or project_root")

    def _binding_id(self, arguments: Mapping[str, Any]) -> str:
        binding_id = arguments.get("binding_id")
        if isinstance(binding_id, str) and binding_id:
            binding = self.store.binding(binding_id)
            if binding["scope"] != "session":
                raise ValidationError("binding is not a session binding")
            return binding_id
        session_id = arguments.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValidationError("session scope requires binding_id or session_id")
        project_id = self._project_id(arguments)
        return self.store.ensure_binding(project_id, session_id)["binding_id"]

    def _scope(self, arguments: Mapping[str, Any]) -> tuple[str | None, str | None]:
        has_project = bool(arguments.get("project_id") or arguments.get("project_root"))
        has_binding = bool(arguments.get("binding_id"))
        has_session_id = bool(arguments.get("session_id"))
        if has_binding and (has_session_id or has_project):
            raise ValidationError(
                "binding scope may not be combined with session id or project context"
            )
        if has_binding:
            return None, self._binding_id(arguments)
        if has_session_id:
            if not has_project:
                raise ValidationError(
                    "session id scope also requires its project context"
                )
            return None, self._binding_id(arguments)
        if not has_project:
            raise ValidationError("mutation requires exactly one explicit project or session scope")
        return self._project_id(arguments), None

    @staticmethod
    def _required(arguments: Mapping[str, Any], name: str) -> Any:
        value = arguments.get(name)
        if value is None or value == "":
            raise ValidationError(f"{name} is required")
        return value

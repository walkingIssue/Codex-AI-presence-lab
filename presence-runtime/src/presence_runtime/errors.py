"""Typed runtime failures surfaced by the CLI and IPC boundary."""


class PresenceError(RuntimeError):
    """Base class for user-visible Presence Runtime errors."""

    code = "presence_error"


class ValidationError(PresenceError):
    """A complete candidate document or event was rejected."""

    code = "validation_error"

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        rendered = f"{path}: {message}" if path else message
        super().__init__(rendered)


class NotFoundError(PresenceError):
    """A requested runtime or catalog identity does not exist."""

    code = "not_found"


class ConflictError(PresenceError):
    """The requested operation conflicts with current authoritative state."""

    code = "conflict"


class CatalogReferenceError(ConflictError):
    """Catalog removal was refused because active records reference the item."""

    code = "catalog_reference"


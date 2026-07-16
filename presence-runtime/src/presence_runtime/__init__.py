"""Authoritative machine-local Presence Runtime."""

from .catalog import Catalog
from .errors import (
    CatalogReferenceError,
    ConflictError,
    NotFoundError,
    PresenceError,
    ValidationError,
)
from .models import EffectiveSnapshot, RendererSettings, SemanticSnapshot, TTSSettings
from .resolver import PresenceResolver
from .renderer import ElectronRendererSupervisor
from .store import PresenceStore

__all__ = [
    "Catalog",
    "CatalogReferenceError",
    "ConflictError",
    "EffectiveSnapshot",
    "ElectronRendererSupervisor",
    "NotFoundError",
    "PresenceError",
    "PresenceResolver",
    "PresenceStore",
    "RendererSettings",
    "SemanticSnapshot",
    "TTSSettings",
    "ValidationError",
]

__version__ = "0.2.0.dev0"

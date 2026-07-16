"""Domain-specific, user-correctable errors."""


class AvatarRuntimeError(RuntimeError):
    """Raised when an avatar input or lifecycle operation is unsafe or invalid."""

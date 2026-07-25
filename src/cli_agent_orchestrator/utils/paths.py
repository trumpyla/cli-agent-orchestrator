"""Filesystem-path helpers shared across services and utils."""

import os
from pathlib import Path

from cli_agent_orchestrator.constants import CAO_HOME_DIR


def _is_at_or_below(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root + os.sep)


def normalized_path(path: "str | Path") -> str:
    """Canonical form for comparing configured directory paths (GH #280/#281).

    Disabled agent-profile directories are stored as the exact strings the UI
    sends, but the same directory can be reached via a different spelling
    (``~``, trailing slash, a symlink; e.g. the local agent-store is also a
    provider default). ``realpath`` + ``expanduser`` canonicalizes all of
    those, so the disable check matches whenever two spellings reach the same
    physical directory.

    Lives here (rather than in ``utils.agent_profiles`` or
    ``services.settings_service``) so both can import it without reaching into
    each other's private API.
    """
    normalized = os.path.realpath(os.path.expanduser(str(path)))
    home = os.path.realpath(os.path.expanduser("~"))
    cao_home = os.path.realpath(str(CAO_HOME_DIR))
    sensitive_roots = (
        os.path.join(home, ".ssh"),
        os.path.join(home, ".gnupg"),
        os.path.join(home, ".aws"),
    )
    if any(_is_at_or_below(normalized, root) for root in sensitive_roots) and not (
        _is_at_or_below(normalized, cao_home)
    ):
        # Never include either the configured or resolved path: callers may
        # safely surface this stable reason code in CLI/API/startup evidence.
        raise ValueError("configured directory rejected: sensitive_root")
    return normalized

"""Durable owner-only atomic text-file persistence."""

import os
import tempfile
from pathlib import Path
from typing import TextIO


def _write_payload(file_object: TextIO, payload: str) -> None:
    file_object.write(payload)


def _flush_file(file_object: TextIO) -> None:
    file_object.flush()


def _sync_fd(file_descriptor: int) -> None:
    os.fsync(file_descriptor)


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def atomic_write_text(path: Path, payload: str) -> None:
    """Replace ``path`` only after an owner-only temporary file is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    descriptor_owned = True
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_object:
            descriptor_owned = False
            _write_payload(file_object, payload)
            _flush_file(file_object)
            _sync_fd(file_object.fileno())
        _replace_path(temporary_path, path)
    except BaseException:
        if descriptor_owned:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)
        raise

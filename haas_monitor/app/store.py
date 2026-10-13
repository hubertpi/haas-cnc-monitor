"""Versioned, atomic persistence; corruption fails closed instead of resetting IDs."""
import json
import os
import tempfile
from pathlib import Path


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "version": 1, "machines": []}
        if self.data.get("version") != 1 or not isinstance(self.data.get("machines"), list):
            raise ValueError("Unsupported or damaged machine configuration; restore a backup")

    def save(self, machines):
        replacement = {"version": 1, "machines": machines}
        atomic_json(self.path, replacement)
        self.data = replacement

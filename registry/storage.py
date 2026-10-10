"""Store opaque bytes; never deserialize a model or extract an archive."""

import hashlib
import os
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

CHUNK_SIZE = 64 * 1024


class EmptyArtifact(Exception):
    pass


class ArtifactTooLarge(Exception):
    pass


class CorruptArtifact(Exception):
    pass


class ArtifactStore:
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, source: BinaryIO) -> dict:
        key = uuid4().hex + ".blob"
        destination = self.root / key
        temporary = self.root / (key + ".tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as target:
                while chunk := source.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactTooLarge(f"Artifact limit is {self.max_bytes} bytes")
                    digest.update(chunk)
                    target.write(chunk)
                if size == 0:
                    raise EmptyArtifact("Artifact must not be empty")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            # Persist the new directory entry on POSIX (Windows has no O_DIRECTORY).
            if hasattr(os, "O_DIRECTORY"):
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return {"artifact_key": key, "sha256": digest.hexdigest(), "artifact_size": size}

    def remove(self, key: str):
        (self.root / key).unlink(missing_ok=True)

    def read_verified(self, version: dict) -> bytes:
        # Demo files are capped at 10 MiB. Buffering lets us verify before sending.
        try:
            with (self.root / version["artifact_key"]).open("rb") as source:
                content = source.read(version["artifact_size"] + 1)
        except FileNotFoundError as exc:
            raise CorruptArtifact("Artifact is missing; restore it from backup") from exc
        if len(content) != version["artifact_size"] or hashlib.sha256(content).hexdigest() != version["sha256"]:
            raise CorruptArtifact("Artifact checksum mismatch; restore it from backup")
        return content

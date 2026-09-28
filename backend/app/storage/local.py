from pathlib import Path
from typing import Protocol
import os
import uuid
from app.core.config import settings
from app.core.errors import DomainError


class Storage(Protocol):
    def write(self, key: str, data: bytes) -> str: ...
    def read(self, key: str) -> bytes: ...


class LocalStorage:
    def __init__(self, root: Path | None = None):
        self.root = (root or settings().storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise DomainError("Invalid storage path")
        return path

    def write(self, key, data):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp-" + uuid.uuid4().hex)
        temp.write_bytes(data)
        os.replace(temp, path)
        return key

    def read(self, key):
        return self.path(key).read_bytes()

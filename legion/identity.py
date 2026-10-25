"""Ed25519 identities: one per agent. LEGION_HOME chooses which one is used."""

import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

PREFIX = "ed25519:"


def home() -> Path:
    return Path(os.environ.get("LEGION_HOME") or Path.home() / ".legion")


class Identity:
    def __init__(self, name: str, key: Ed25519PrivateKey):
        self.name = name
        self._key = key
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.author = PREFIX + raw.hex()

    @property
    def short(self) -> str:
        return short(self.author)

    def sign(self, data: bytes) -> str:
        return self._key.sign(data).hex()

    @classmethod
    def generate(cls, name: str) -> "Identity":
        return cls(name, Ed25519PrivateKey.generate())

    def save(self, path: Path | None = None) -> Path:
        path = path or home() / "identity.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        path.write_text(json.dumps({"name": self.name, "key": raw.hex()}), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "Identity | None":
        path = path or home() / "identity.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["name"], Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["key"])))


def short(author: str) -> str:
    return author[len(PREFIX):len(PREFIX) + 8]


def verify(author: str, data: bytes, sig: str) -> bool:
    if not isinstance(author, str) or not author.startswith(PREFIX) or not isinstance(sig, str):
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(author[len(PREFIX):]))
        key.verify(bytes.fromhex(sig), data)
        return True
    except (InvalidSignature, ValueError):
        return False

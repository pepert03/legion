"""Ed25519 identities: one per agent.

Every identity is its own file, home()/identities/<id>.json, and is never overwritten: creating one only ever
adds a file. Each repository is bound to the identity that works in it (`legion.identity` in its git config),
so on a computer shared by several agents and an integrator, an agent can't take over anyone else's
identity by creating its own. A repository that isn't bound uses home()/identity.json, the old single
identity. LEGION_HOME moves home() elsewhere.
"""

import json
import os
import subprocess
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

PREFIX = "ed25519:"


def home() -> Path:
    return Path(os.environ.get("LEGION_HOME") or Path.home() / ".legion")


def identities_dir() -> Path:
    return home() / "identities"


class Identity:
    def __init__(self, name: str, key: Ed25519PrivateKey):
        self.name = name
        self._key = key
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.author = PREFIX + raw.hex()

    @property
    def short(self) -> str:
        return short(self.author)

    @property
    def key_id(self) -> str:
        """The name of this identity's file: 16 hex characters of its public key."""
        return self.author[len(PREFIX):len(PREFIX) + 16]

    def sign(self, data: bytes) -> str:
        return self._key.sign(data).hex()

    @classmethod
    def generate(cls, name: str) -> "Identity":
        return cls(name, Ed25519PrivateKey.generate())

    def _data(self) -> str:
        raw = self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return json.dumps({"name": self.name, "key": raw.hex()})

    def save(self, path: Path | None = None) -> Path:
        """Store it in its own file (or at `path`). A file holding a different key is never replaced."""
        path = path or identities_dir() / f"{self.key_id}.json"
        if path.exists():
            existing = Identity.load(path)
            if existing and existing.author != self.author:
                raise FileExistsError(f"{path} holds another identity ({existing.name}); not replacing it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._data(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None, repo=None) -> "Identity | None":
        """The identity at `path`; or the one `repo` is bound to; or, if it isn't bound, home()/identity.json."""
        if path is None:
            bound = bound_id(repo) if repo is not None else None
            path = identities_dir() / f"{bound}.json" if bound else home() / "identity.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["name"], Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["key"])))


def bound_id(repo) -> str | None:
    p = subprocess.run(["git", "config", "--get", "legion.identity"], cwd=repo, capture_output=True, text=True)
    return p.stdout.strip() or None if p.returncode == 0 else None


def bind(repo, ident: Identity) -> None:
    """Make `ident` the identity that works in this repository (it's saved in its own file first)."""
    ident.save()
    subprocess.run(["git", "config", "legion.identity", ident.key_id], cwd=repo, check=True, capture_output=True)
    # Commits here are signed as this identity, not as the person's global git account: the project is public,
    # and their email would go out with every patch.
    for key, value in (("user.name", ident.name), ("user.email", f"{ident.key_id}@legion.local")):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True, capture_output=True)


def all_identities() -> list[Identity]:
    found = {}
    for f in sorted(identities_dir().glob("*.json")) + [home() / "identity.json"]:
        ident = Identity.load(f) if f.exists() else None
        if ident:
            found.setdefault(ident.author, ident)
    return list(found.values())


def find(ref: str) -> list[Identity]:
    """Identities whose name or key starts with `ref`."""
    return [i for i in all_identities() if i.name == ref or i.key_id.startswith(ref) or i.short == ref]


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

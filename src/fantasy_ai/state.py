"""Encrypted run state for CI. data/ (pending approvals, news cache, telegram offset, last run)
is packed into one Fernet-encrypted file so it can live in actions/cache on a public repo
without being readable. Key: STATE_KEY secret (generate with `fai state keygen`)."""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from .config import ROOT

DATA_DIR = ROOT / "data"
STATE_FILE = ROOT / ".state" / "state.enc"


def _fernet(key: str | None = None):
    from cryptography.fernet import Fernet
    key = key or os.environ.get("STATE_KEY", "")
    if not key:
        raise RuntimeError("STATE_KEY is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def keygen() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def pack(data_dir: Path = DATA_DIR, out: Path = STATE_FILE, key: str | None = None) -> int:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if data_dir.exists():
            tar.add(data_dir, arcname="data")
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = _fernet(key).encrypt(buf.getvalue())
    out.write_bytes(blob)
    return len(blob)


def unpack(src: Path = STATE_FILE, data_dir: Path = DATA_DIR, key: str | None = None) -> bool:
    if not src.exists():
        return False
    raw = _fernet(key).decrypt(src.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name == "data" or m.name.startswith("data/")]
        for m in members:
            if m.issym() or m.islnk() or ".." in Path(m.name).parts:
                raise RuntimeError("unsafe path in state archive")
        tar.extractall(data_dir.parent, members=members, filter="data")
    return True

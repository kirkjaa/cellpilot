"""JSON storage: a local folder or a Cloud Storage bucket behind one interface, plus the
read-only sample library shipped under samples/.

CELLPILOT_STORE=./data          -> LocalStore (development)
CELLPILOT_STORE=gs://bucket[/p] -> GcsStore (production)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional, Protocol

ROOT = Path(__file__).resolve().parent.parent
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def analysis_key(sid: str, aid: str) -> str:
    return f"sessions/{sid}/analyses/{aid}.json"


def analyses_prefix(sid: str) -> str:
    return f"sessions/{sid}/analyses/"


def argon_key(sid: str, aid: str) -> str:
    return f"sessions/{sid}/argon/{aid}.json"


class Store(Protocol):
    is_bucket: bool

    def read_json(self, key: str) -> Optional[dict]: ...
    def write_json(self, key: str, data: dict) -> None: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def describe(self) -> str: ...


class LocalStore:
    is_bucket = False

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def describe(self) -> str:
        return f"local folder {self.root}"

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"key outside the store: {key}")
        return path

    def read_json(self, key: str) -> Optional[dict]:
        path = self._path(key)
        for attempt in range(5):  # Windows: a concurrent os.replace can briefly deny access
            try:
                return json.loads(path.read_text("utf-8"))
            except FileNotFoundError:
                return None
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        return json.loads(path.read_text("utf-8"))

    def write_json(self, key: str, data: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, path)

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*.json")
                      if not p.name.startswith("."))


class GcsStore:
    is_bucket = True

    def __init__(self, url: str):
        from google.cloud import storage

        bucket, _, prefix = url[len("gs://"):].partition("/")
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self.client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
        self.bucket = self.client.bucket(bucket)

    def describe(self) -> str:
        return f"gs://{self.bucket_name}/{self.prefix}".rstrip("/")

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_json(self, key: str) -> Optional[dict]:
        from google.api_core.exceptions import NotFound

        try:
            return json.loads(self.bucket.blob(self._name(key)).download_as_bytes())
        except NotFound:
            return None

    def write_json(self, key: str, data: dict) -> None:
        blob = self.bucket.blob(self._name(key))
        blob.cache_control = "no-store"
        blob.upload_from_string(json.dumps(data, indent=2, ensure_ascii=False),
                                content_type="application/json")

    def list_keys(self, prefix: str) -> list[str]:
        cut = len(self.prefix) + 1 if self.prefix else 0
        return sorted(b.name[cut:] for b in self.client.list_blobs(self.bucket, prefix=self._name(prefix))
                      if b.name.endswith(".json"))

    def media_uri(self, key: str, local: Path) -> str:
        """Upload a local clip once (skipped when the same size is already there); return its gs:// URI."""
        name = self._name(key)
        existing = self.bucket.get_blob(name)  # None when missing
        if existing is None or existing.size != local.stat().st_size:
            self.bucket.blob(name).upload_from_filename(str(local), content_type="video/mp4")
        return f"gs://{self.bucket_name}/{name}"


def make_store(spec: Optional[str] = None) -> Store:
    spec = spec or os.environ.get("CELLPILOT_STORE") or str(ROOT / "data")
    if spec.startswith("gs://"):
        return GcsStore(spec)
    return LocalStore(spec)


class Samples:
    """Read-only sessions shipped under samples/<sid>/ with their clips and stored analyses."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("CELLPILOT_SAMPLES") or ROOT / "samples").resolve()

    def dir(self, sid: str) -> Optional[Path]:
        if not ID_RE.match(sid):
            return None
        path = self.root / sid
        return path if (path / "session.json").is_file() else None

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if ID_RE.match(p.name) and (p / "session.json").is_file())

    def session(self, sid: str) -> Optional[dict]:
        from .schemas import Session

        d = self.dir(sid)
        if d is None:
            return None
        return Session.model_validate_json((d / "session.json").read_text("utf-8")).model_dump()

    def clip_path(self, sid: str, file: str) -> Optional[Path]:
        session = self.session(sid)
        if session is None or file not in {c["file"] for c in session["clips"]}:
            return None
        path = self.root / sid / file
        return path if path.is_file() else None

    def analyses(self, sid: str) -> list[dict]:
        d = self.dir(sid)
        if d is None or not (d / "analyses").is_dir():
            return []
        return [json.loads(p.read_text("utf-8")) for p in sorted((d / "analyses").glob("*.json"))]

    def analysis(self, sid: str, aid: str) -> Optional[dict]:
        d = self.dir(sid)
        if d is None or not ID_RE.match(aid):
            return None
        path = d / "analyses" / f"{aid}.json"
        return json.loads(path.read_text("utf-8")) if path.is_file() else None

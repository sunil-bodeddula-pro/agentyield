"""The store: plain JSON files in a folder, or in a gs:// or s3:// bucket. There is no database.

    <store>/v1/
      repos/<owner>/<repo>/prs/<number>.json   one PR: filtered facts and its timeline summary
      repos/<owner>/<repo>/cursor.json         newest update seen and the list ETag, so a sync fetches only changes
      config/<hash>.yaml                       every config that was applied, by content hash
      manifest.json                            applied config, tracked and hidden repositories, last sync
      .lease                                   which machine is writing; one writer at a time

Everything written here has already passed the guardrail filter (guardrails.py). Cloud clients are optional
extras, imported only when a gs:// or s3:// store is used.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
import uuid
from typing import Any, Protocol

FORMAT = "v1"
LEASE_SECONDS = 15 * 60


class Backend(Protocol):
    def get(self, rel: str) -> bytes | None: ...

    def put(self, rel: str, data: bytes) -> None: ...

    def list(self, prefix: str) -> list[str]: ...

    def delete(self, rel: str) -> None: ...


class FileBackend:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    def _path(self, rel: str) -> str:
        path = os.path.normpath(os.path.join(self.root, rel))
        if not path.startswith(self.root + os.sep) and path != self.root:
            raise ValueError(f"path escapes the store: {rel}")
        return path

    def get(self, rel: str) -> bytes | None:
        try:
            with open(self._path(rel), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def put(self, rel: str, data: bytes) -> None:
        path = self._path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def list(self, prefix: str) -> list[str]:
        top = self._path(prefix) if prefix else self.root
        out: list[str] = []
        for dirpath, _, names in os.walk(top):
            for n in names:
                if not n.endswith(".tmp"):
                    out.append(os.path.relpath(os.path.join(dirpath, n), self.root).replace(os.sep, "/"))
        return sorted(out)

    def delete(self, rel: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(self._path(rel))


class GCSBackend:
    def __init__(self, bucket: str, prefix: str, client: Any = None):
        if client is None:
            try:
                from google.cloud import storage  # noqa: PLC0415  optional extra
            except ImportError as exc:
                raise SystemExit("gs:// needs the Google Cloud Storage client: pip install 'agentyield[gcs]'") from exc
            client = storage.Client()
        self.client = client
        self.bucket = client.bucket(bucket)
        self.prefix = prefix.strip("/")

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def get(self, rel: str) -> bytes | None:
        blob = self.bucket.blob(self._key(rel))
        return bytes(blob.download_as_bytes()) if blob.exists() else None

    def put(self, rel: str, data: bytes) -> None:
        self.bucket.blob(self._key(rel)).upload_from_string(data, content_type="application/json")

    def list(self, prefix: str) -> list[str]:
        cut = len(self.prefix) + 1 if self.prefix else 0
        return sorted(b.name[cut:] for b in self.client.list_blobs(self.bucket, prefix=self._key(prefix)))

    def delete(self, rel: str) -> None:
        blob = self.bucket.blob(self._key(rel))
        if blob.exists():
            blob.delete()


class S3Backend:
    def __init__(self, bucket: str, prefix: str, client: Any = None):
        if client is None:
            try:
                import boto3  # noqa: PLC0415  optional extra
            except ImportError as exc:
                raise SystemExit("s3:// needs boto3: pip install 'agentyield[s3]'") from exc
            client = boto3.client("s3")  # honours AWS_ENDPOINT_URL, AWS_PROFILE, AWS_REGION
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def get(self, rel: str) -> bytes | None:
        try:
            return bytes(self.client.get_object(Bucket=self.bucket, Key=self._key(rel))["Body"].read())
        except Exception as exc:  # botocore ClientError; only a missing key means "no file"
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, rel: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(rel), Body=data, ContentType="application/json")

    def list(self, prefix: str) -> list[str]:
        cut = len(self.prefix) + 1 if self.prefix else 0
        out: list[str] = []
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix))
        for page in pages:
            out.extend(obj["Key"][cut:] for obj in page.get("Contents", []))
        return sorted(out)

    def delete(self, rel: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(rel))


def backend_for(uri: str) -> Backend:
    if uri.startswith("gs://"):
        bucket, _, prefix = uri[5:].partition("/")
        return GCSBackend(bucket, prefix)
    if uri.startswith("s3://"):
        bucket, _, prefix = uri[5:].partition("/")
        return S3Backend(bucket, prefix)
    if uri.startswith("file://"):
        return FileBackend(uri[7:])
    if "://" in uri:
        raise SystemExit(f"unsupported store: {uri} (use a folder, gs:// or s3://)")
    return FileBackend(uri)


def dumps(data: Any) -> bytes:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False).encode()


class LeaseHeld(RuntimeError):
    pass


class Store:
    def __init__(self, uri: str, backend: Backend | None = None, holder: str | None = None):
        self.uri = uri
        self.backend = backend or backend_for(uri)
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"

    # ---------- JSON files ----------
    def read(self, rel: str, default: Any = None) -> Any:
        raw = self.backend.get(f"{FORMAT}/{rel}")
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def write(self, rel: str, data: Any) -> None:
        self.backend.put(f"{FORMAT}/{rel}", dumps(data))

    def put_bytes(self, rel: str, data: bytes) -> None:
        self.backend.put(f"{FORMAT}/{rel}", data)

    def get_bytes(self, rel: str) -> bytes | None:
        return self.backend.get(f"{FORMAT}/{rel}")

    def files(self, prefix: str = "") -> list[str]:
        cut = len(FORMAT) + 1
        return [f[cut:] for f in self.backend.list(f"{FORMAT}/{prefix}")]

    def delete(self, rel: str) -> None:
        self.backend.delete(f"{FORMAT}/{rel}")

    # ---------- layout ----------
    @staticmethod
    def repo_dir(repo: str) -> str:
        return f"repos/{repo}"

    def pr_path(self, repo: str, number: int) -> str:
        return f"{self.repo_dir(repo)}/prs/{number}.json"

    def pr_files(self, repo: str) -> list[str]:
        return [f for f in self.files(f"{self.repo_dir(repo)}/prs/") if f.endswith(".json")]

    def manifest(self) -> dict[str, Any]:
        m = self.read("manifest.json", {}) or {}
        m.setdefault("repositories", {})
        return dict(m)

    def save_manifest(self, m: dict[str, Any]) -> None:
        self.write("manifest.json", m)

    # ---------- one writer at a time ----------
    def acquire(self, seconds: int = LEASE_SECONDS) -> None:
        now = time.time()
        lease = self.read(".lease")
        if lease and lease.get("holder") != self.holder and lease.get("expires", 0) > now:
            raise LeaseHeld(
                f"the store is being written by {lease['holder']} until "
                f"{time.strftime('%H:%M:%S', time.localtime(lease['expires']))}"
            )
        self.write(".lease", {"holder": self.holder, "expires": now + seconds})

    def release(self) -> None:
        lease = self.read(".lease")
        if lease and lease.get("holder") == self.holder:
            self.delete(".lease")

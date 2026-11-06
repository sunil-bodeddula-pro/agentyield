from __future__ import annotations

import io
import tempfile
import unittest
from typing import Any, ClassVar

import helpers  # noqa: F401  first: puts src on the path

# isort: split
from agentyield.guardrails import Filter
from agentyield.store import FileBackend, GCSBackend, LeaseHeld, S3Backend, Store


class FilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Filter(deny_terms=["Globex", "Project Bluebird"], extra_secret_patterns=[r"ACME-\d{6}"])

    def clean(self, text: str) -> str:
        return self.f.clean(text)[0]

    def test_secrets(self) -> None:
        samples = {
            "ghp_" + "x" * 36: "github token",
            "github_pat_" + "a1" * 30: "fine-grained token",
            "AKIAIOSFODNN7EXAMPLE": "aws key",
            "xoxb-1234567890-abcdefghij": "slack",
            "sk-ant-" + "a" * 30: "api key",
            "postgres://admin:hunter22@db:5432/app": "url credentials",
            "password=Sup3rS3cret!": "assignment",
            "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----": "private key",
            "Zx8Kq2Lm9Vb4Np7Rt1Wy6Ys3Hd5Jf0Gc": "high entropy",
            "ticket ACME-123456": "custom pattern",
        }
        for text, why in samples.items():
            self.assertIn("[REDACTED:secret]", self.clean(text), why)

    def test_personal_and_internal(self) -> None:
        self.assertEqual(self.clean("mail jane.doe@acme.com"), "mail [REDACTED:email]")
        self.assertEqual(self.clean("call +44 20 7946 0958"), "call [REDACTED:phone]")
        self.assertEqual(self.clean("call (555) 123-4567"), "call [REDACTED:phone]")
        self.assertEqual(self.clean("on db1.acme.internal"), "on [REDACTED:host]")
        self.assertEqual(self.clean("see /home/jdoe/x"), "see [REDACTED:path]/x")
        self.assertEqual(self.clean("from 10.0.0.12"), "from [REDACTED:ip]")
        self.assertEqual(self.clean("card 4111 1111 1111 1111"), "card [REDACTED:card]")
        self.assertEqual(
            self.clean("Globex renewal for project bluebird"), "[REDACTED:term] renewal for [REDACTED:term]"
        )

    def test_ordinary_text_is_untouched(self) -> None:
        for text in (
            "fix: release 2026-11-05 build",
            "feat(api): add v2.3 endpoint (#123)",
            "revert " + "a1b2c3d4" * 5,
            "chore: bump lodash from 4.17.20 to 4.17.21",
            "refactor PaymentIntentService",
        ):
            self.assertEqual(self.clean(text), text)

    def test_fails_closed(self) -> None:
        f = Filter()
        f._compiled = [("secret", None)]  # type: ignore[list-item]  # a broken pattern
        self.assertEqual(f.clean("anything"), ("", ["filter-error"]))

    def test_restricted_labels(self) -> None:
        self.assertTrue(Filter(restricted_labels=["security"]).restricted_label(["Security"]))


class FakeGCS:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def bucket(self, name: str) -> Any:
        fake = self

        class Blob:
            def __init__(self, key: str):
                self.name = key

            def exists(self) -> bool:
                return self.name in fake.objects

            def download_as_bytes(self) -> bytes:
                return fake.objects[self.name]

            def upload_from_string(self, data: bytes, content_type: str = "") -> None:
                fake.objects[self.name] = data

            def delete(self) -> None:
                fake.objects.pop(self.name)

        class Bucket:
            def blob(self, key: str) -> Blob:
                return Blob(key)

        return Bucket()

    def list_blobs(self, bucket: Any, prefix: str = "") -> list[Any]:
        return [bucket.blob(k) for k in sorted(self.objects) if k.startswith(prefix)]


class FakeS3:
    class Missing(Exception):
        response: ClassVar[dict[str, Any]] = {"Error": {"Code": "NoSuchKey"}}

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> None:
        self.objects[Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise self.Missing()
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)

    def get_paginator(self, name: str) -> Any:
        fake = self

        class P:
            def paginate(self, Bucket: str, Prefix: str) -> Any:
                yield {"Contents": [{"Key": k} for k in sorted(fake.objects) if k.startswith(Prefix)]}

        return P()


class StoreTest(unittest.TestCase):
    def round_trip(self, store: Store) -> None:
        store.write("repos/acme/x/prs/1.json", {"title": "t"})
        self.assertEqual(store.read("repos/acme/x/prs/1.json"), {"title": "t"})
        self.assertEqual(store.read("missing.json", "default"), "default")
        self.assertEqual(store.pr_files("acme/x"), ["repos/acme/x/prs/1.json"])
        store.acquire()
        other = Store(store.uri, store.backend, holder="other:1")
        with self.assertRaises(LeaseHeld):
            other.acquire()
        store.release()
        other.acquire()

    def test_folder(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.round_trip(Store(d))
            with self.assertRaises(ValueError):
                FileBackend(d).put("../escape.json", b"x")

    def test_gcs_with_prefix(self) -> None:
        fake = FakeGCS()
        self.round_trip(Store("gs://b/team/ay", GCSBackend("b", "team/ay", client=fake)))
        self.assertTrue(all(k.startswith("team/ay/v1/") for k in fake.objects))

    def test_s3(self) -> None:
        self.round_trip(Store("s3://b", S3Backend("b", "", client=FakeS3())))

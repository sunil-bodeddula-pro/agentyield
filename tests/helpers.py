from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentyield.cli import main
from mockgh import PLANTED, TOKEN, FakeGitHub, Org

CONFIG = """\
version: 1
store: {store}
github:
  api_url: {api}
repositories:
  include:
    - name: acme/checkout-service
      team: payments
    - name: acme/web-dashboard
      team: web
    - name: acme/security-patches
      team: platform
      restricted: true
guardrails:
  deny_terms: ["{deny}"]
"""


class OrgCase(unittest.TestCase):
    """A fake GitHub, a temporary store and config, and a way to run the CLI against them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.org = Org()
        self.gh = FakeGitHub(self.org)
        self.store = os.path.join(self.dir, "store")
        self.config = os.path.join(self.dir, "agentyield.yaml")
        self.write_config(CONFIG)
        self._env = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "AGENTYIELD_GITHUB_API", "AGENTYIELD_CONFIG")}
        os.environ["GITHUB_TOKEN"] = TOKEN
        os.environ["AGENTYIELD_GITHUB_API"] = self.gh.url
        os.environ.pop("AGENTYIELD_CONFIG", None)

    def tearDown(self) -> None:
        self.gh.close()
        self.tmp.cleanup()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_config(self, template: str, **extra: Any) -> None:
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(template.format(store=self.store, api=self.gh.url, deny=PLANTED["deny_term"], **extra))

    def run_cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--config", self.config, *args])
            except SystemExit as exc:  # how the CLI reports a fatal problem
                print(exc, file=err)
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue() + err.getvalue()

    def store_bytes(self) -> bytes:
        blob = b""
        for dirpath, _, names in os.walk(self.store):
            for n in names:
                with open(os.path.join(dirpath, n), "rb") as fh:
                    blob += fh.read()
        return blob

    def assert_no_planted(self, text: bytes | str, allow: tuple[str, ...] = ()) -> None:
        blob = text.encode() if isinstance(text, str) else text
        for name, value in PLANTED.items():
            if name not in allow:
                self.assertNotIn(value.encode(), blob, f"planted {name} leaked")

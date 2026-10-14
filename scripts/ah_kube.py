"""Thin kubectl wrapper. Every call passes an explicit --context. Errors carry the verb
and the first line of stderr only: argv holds patch bodies and may hold secrets."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Iterator

APP_KIND = "applications.argoproj.io"


class KubectlError(Exception):
    def __init__(self, verb: str, returncode: int, stderr: str):
        self.verb = verb
        self.returncode = returncode
        self.first_line = (stderr.strip().splitlines() or [""])[0][:200]
        self.not_found = "NotFound" in stderr
        super().__init__(f"kubectl {verb} failed (exit {returncode}): {self.first_line}")


def iter_json_docs(lines) -> Iterator[dict]:
    """Yield each top-level JSON document from a text stream. Accepts compact NDJSON
    (what `kubectl get -w --output-watch-events -o json` emits) and indented documents
    printed back to back: accumulate lines until one parses."""
    dec = json.JSONDecoder()
    buf = ""
    for line in lines:
        buf += line
        while True:
            s = buf.lstrip()
            if not s:
                buf = ""
                break
            try:
                doc, end = dec.raw_decode(s)
            except json.JSONDecodeError:
                break
            yield doc
            buf = s[end:]


def _ns(ns):
    return ["-n", ns] if ns else []


class Kube:
    def __init__(self, context: str, kubectl: str | None = None):
        self.context = context
        self.kubectl = kubectl or os.environ.get("ARGO_HOLD_KUBECTL", "kubectl")

    def _base(self) -> list[str]:
        return [self.kubectl, "--context", self.context]

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        cp = subprocess.run(self._base() + args, input=stdin, capture_output=True, text=True)
        if cp.returncode != 0:
            raise KubectlError(args[0], cp.returncode, cp.stderr)
        return cp.stdout

    def get(self, kind, name, ns=None):
        try:
            return json.loads(self._run(["get", kind, name, *_ns(ns), "-o", "json"]))
        except KubectlError as e:
            if e.not_found:
                return None
            raise

    def patch(self, kind, name, ns, ptype, doc, manager, dry_run=False):
        args = ["patch", kind, name, *_ns(ns), "--type", ptype, "-p", json.dumps(doc),
                "--field-manager", manager, "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return json.loads(self._run(args))

    def apply_ssa(self, manifest, manager, dry_run=False):
        args = ["apply", "--server-side", "--field-manager", manager, "--force-conflicts",
                "-f", "-", "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return json.loads(self._run(args, stdin=json.dumps(manifest)))

    def delete(self, kind, name, ns=None):
        self._run(["delete", kind, name, *_ns(ns)])

    def annotate(self, kind, name, ns, key, value):
        self._run(["annotate", kind, name, *_ns(ns), f"{key}={value}", "--overwrite"])

    def convert_manifest(self, path: str) -> list[dict]:
        """YAML/JSON file -> list of objects, converted client-side (nothing is sent)."""
        doc = json.loads(self._run(["create", "--dry-run=client", "-f", path, "-o", "json"]))
        return doc["items"] if doc.get("kind") == "List" else [doc]

    def watch(self, kind, name, ns=None) -> subprocess.Popen:
        return subprocess.Popen(
            self._base() + ["get", kind, name, *_ns(ns), "-w", "--output-watch-events", "-o", "json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

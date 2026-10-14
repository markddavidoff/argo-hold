"""In-memory fake cluster shared by the fake kubectl shim and the tests.

State is one JSON file guarded by flock, so the shim (a new process per call) and the
test process see the same cluster. Shapes are anchored to output recorded from real
kubectl on k3d (tests/fixtures/recorded); test_kube.py checks the contract.
"""
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path

CTX = "test-ctx"

_ALIASES = {"deploy": "deployment", "ds": "daemonset", "app": "application", "cm": "configmap",
            "applications": "application", "deployments": "deployment",
            "daemonsets": "daemonset", "configmaps": "configmap", "secrets": "secret"}


def norm_kind(kind):
    k = kind.lower().split(".")[0]
    return _ALIASES.get(k, k)


def core(obj):
    """The object minus what a no-op patch would not change."""
    o = copy.deepcopy(obj)
    md = o.get("metadata", {})
    md.pop("resourceVersion", None)
    md.pop("managedFields", None)
    return o


def make_app(name="otel-collector", automated=None, phase=None):
    automated = {"prune": True, "selfHeal": True} if automated is None else automated
    sync_policy = {"syncOptions": ["CreateNamespace=true"]}
    if automated != "off":
        sync_policy["automated"] = automated
    app = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
           "metadata": {"name": name, "namespace": "argo-cd"},
           "spec": {"project": "default", "syncPolicy": sync_policy},
           "status": {"sync": {"status": "Synced"}}}
    if phase:
        app["status"]["operationState"] = {"phase": phase}
    return app


def make_daemonset(name="otel-collector-logs", ns="monitoring"):
    return {"apiVersion": "apps/v1", "kind": "DaemonSet",
            "metadata": {"name": name, "namespace": ns},
            "spec": {"minReadySeconds": 5,
                     "template": {"spec": {
                         "containers": [{"name": "otel", "image": "otel:1", "args": ["--a"]}],
                         "tolerations": [{"key": "x", "operator": "Exists"}]}}},
            "status": {"numberReady": 18}}


def _key(ctx, kind, ns, name):
    return f"{ctx}|{norm_kind(kind)}|{ns or ''}|{name}"


def _obj_key(ctx, obj):
    md = obj["metadata"]
    return _key(ctx, obj["kind"], md.get("namespace"), md["name"])


def _set_manager(md, manager, op):
    mf = [m for m in md.get("managedFields", []) if m.get("manager") != manager]
    mf.append({"manager": manager, "operation": op})
    md["managedFields"] = mf


class FakeCluster:
    def __init__(self, path):
        self.path = Path(path)
        self.lockfile = Path(str(path) + ".lock")

    @contextlib.contextmanager
    def locked(self):
        with open(self.lockfile, "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"rv": 0, "objects": {}, "calls": [], "fail": {}}

    def _save(self, s):
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(s))
        os.replace(tmp, self.path)

    def _put(self, s, ctx, obj, manager, op):
        s["rv"] += 1
        obj = copy.deepcopy(obj)
        md = obj.setdefault("metadata", {})
        md["resourceVersion"] = str(s["rv"])
        if manager:
            _set_manager(md, manager, op)
        s["objects"][_obj_key(ctx, obj)] = obj
        return obj

    def put(self, ctx, obj, manager=None, op="Update"):
        with self.locked():
            s = self._load()
            out = self._put(s, ctx, obj, manager, op)
            self._save(s)
            return copy.deepcopy(out)

    seed = put

    def get(self, ctx, kind, ns, name):
        with self.locked():
            o = self._load()["objects"].get(_key(ctx, kind, ns, name))
            return copy.deepcopy(o)

    def update(self, ctx, kind, ns, name, new, manager):
        """Persist `new` unless it is a no-op; returns the stored (or unchanged) object."""
        with self.locked():
            s = self._load()
            old = s["objects"].get(_key(ctx, kind, ns, name))
            if old is not None and core(old) == core(new):
                return copy.deepcopy(old)
            out = self._put(s, ctx, new, manager, "Update" if old else "Apply")
            self._save(s)
            return copy.deepcopy(out)

    def mutate(self, ctx, kind, ns, name, fn):
        with self.locked():
            s = self._load()
            obj = copy.deepcopy(s["objects"][_key(ctx, kind, ns, name)])
            fn(obj)
            self._put(s, ctx, obj, None, None)
            self._save(s)

    def delete(self, ctx, kind, ns, name):
        with self.locked():
            s = self._load()
            gone = s["objects"].pop(_key(ctx, kind, ns, name), None) is not None
            if gone:
                s["rv"] += 1
            self._save(s)
            return gone

    def record(self, argv):
        with self.locked():
            s = self._load()
            s["calls"].append(list(argv))
            self._save(s)

    def calls(self):
        with self.locked():
            return self._load()["calls"]

    def fail_next(self, verb, n=1, skip=0):
        with self.locked():
            s = self._load()
            s["fail"][verb] = {"skip": skip, "n": n}
            self._save(s)

    def should_fail(self, verb):
        with self.locked():
            s = self._load()
            f = s["fail"].get(verb)
            if not f:
                return False
            if f["skip"] > 0:
                f["skip"] -= 1
                self._save(s)
                return False
            if f["n"] <= 0:
                return False
            f["n"] -= 1
            self._save(s)
            return True

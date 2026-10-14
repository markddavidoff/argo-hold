# RBAC for argo-hold

`argo-hold` acts as whoever runs it: it calls `kubectl` with your kubeconfig and context,
so it needs permission on two things — the Argo CD `Application`, and every object you
target. This page lists the minimal verbs and resources, derived from reading the `kubectl`
calls in `scripts/`.

**This Role is derived from the code, not tested against a restricted ServiceAccount.** The
verbs below cover every call the tool makes, but no run has yet confirmed the tool works
with exactly this Role and nothing more. Treat it as a starting point and widen it if a
call is denied.

## What the tool calls

Every `kubectl` verb the tool issues, and why (see `scripts/ah_kube.py` and `scripts/ah_ops.py`):

| `kubectl` call | On | Needs |
|---|---|---|
| `get <app>` / `get <app> -w` | the `Application` | `get`, `list`, `watch` |
| `patch <app>` (auto-sync off, restore, `annotate` refresh) | the `Application` | `patch` |
| `get <target>` / `get <target> -w` | each target object | `get`, `list`, `watch` |
| `patch <target>` (apply and revert the change) | each target object | `patch` |
| `apply --server-side <target>` (`-f` creates a new object) | each target object | `create`, `patch` |
| `delete <target>` (revert of an object the hold created) | each target object | `delete` |

`kubectl get <name> -w` issues a `list` with a field selector and then a `watch`, so `-w`
needs both `list` and `watch`. `annotate` is a `patch` under the hood. `convert_manifest`
(`create --dry-run=client`) runs entirely client-side and needs no cluster permission.

Grant `create` and `delete` on a target only when you use `-f` to create objects; a plain
`--patch` or `--patch-file` hold needs only `get`, `list`, `watch`, `patch`.

## Role for the Argo CD Application

In the namespace Argo CD runs in (`argo-cd` by default; `argocd` for a stock install):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argo-hold-application
  namespace: argo-cd
rules:
  - apiGroups: ["argoproj.io"]
    resources: ["applications"]
    verbs: ["get", "list", "watch", "patch"]
```

## Role for the target objects

In the namespace your targets live in. Replace the resources with the kinds you target;
the example covers `deployments`, `daemonsets`, and `configmaps`:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argo-hold-targets
  namespace: my-namespace
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "daemonsets"]
    verbs: ["get", "list", "watch", "patch", "create", "delete"]
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch", "patch", "create", "delete"]
```

Bind each Role to the user or ServiceAccount that runs `argo-hold` with a `RoleBinding` in
the same namespace. The tool never targets `Secret` objects (it refuses them), so no Role
needs `secrets`.

## Check it

`kubectl auth can-i` reports whether the account can make each call, without making it:

```bash
# The Application, in the Argo CD namespace
kubectl auth can-i get   applications.argoproj.io -n argo-cd
kubectl auth can-i watch applications.argoproj.io -n argo-cd
kubectl auth can-i patch applications.argoproj.io -n argo-cd

# A target, in its namespace (deployment shown)
kubectl auth can-i patch  deployments.apps -n my-namespace
kubectl auth can-i create deployments.apps -n my-namespace   # only if you use -f
kubectl auth can-i delete deployments.apps -n my-namespace   # only if you use -f
```

Each prints `yes` or `no`. Pass `--as <user>` or `--as=system:serviceaccount:<ns>:<name>`
to check another account's access.

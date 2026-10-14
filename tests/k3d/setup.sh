#!/usr/bin/env bash
# Throwaway k3d cluster with Argo CD 2.12.4 in namespace argo-cd and a toy app.
# Never touches the user's current kube context: every kubectl call is explicit.
set -euo pipefail
CLUSTER="${ARGO_HOLD_K3D_CLUSTER:-argo-hold-test}"
[[ "$CLUSTER" =~ ^argo-hold-[a-z0-9-]+$ ]] || { echo "refusing: cluster name '$CLUSTER' must match ^argo-hold-[a-z0-9-]+$" >&2; exit 1; }
CTX="k3d-${CLUSTER}"
HERE="$(cd "$(dirname "$0")" && pwd)"
K=(kubectl --context "$CTX")

# Refuse to write unless the context points at a local API server.
assert_local_context() {
  local server host
  server="$(kubectl --context "$CTX" config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  host="${server#*://}"; host="${host%%[:/]*}"
  case "$host" in
    127.0.0.1|localhost|0.0.0.0) ;;
    *) echo "refusing: context $CTX server '${server:-none}' is not local" >&2; exit 1 ;;
  esac
}

if ! k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster create "$CLUSTER" --wait --kubeconfig-update-default=true --kubeconfig-switch-context=false
fi
assert_local_context
echo "target: context $CTX"
"${K[@]}" create namespace argo-cd --dry-run=client -o yaml | "${K[@]}" apply -f -
curl -fsSL https://raw.githubusercontent.com/argoproj/argo-cd/v2.12.4/manifests/install.yaml \
  | sed 's/namespace: argocd/namespace: argo-cd/' \
  | "${K[@]}" apply -n argo-cd --server-side --force-conflicts -f -
"${K[@]}" -n argo-cd rollout status deploy/argocd-repo-server --timeout=300s
"${K[@]}" -n argo-cd rollout status statefulset/argocd-application-controller --timeout=300s
"${K[@]}" apply -f "$HERE/toy-app.yaml"
for _ in $(seq 1 60); do
  s="$("${K[@]}" -n argo-cd get applications.argoproj.io toy -o jsonpath='{.status.sync.status}' 2>/dev/null || true)"
  [[ "$s" == "Synced" ]] && break
  sleep 5
done
[[ "$s" == "Synced" ]] || { echo "toy app never reached Synced (status: ${s:-none})" >&2; exit 1; }
"${K[@]}" -n toy rollout status deploy/guestbook-ui --timeout=120s
echo "ready: context $CTX"

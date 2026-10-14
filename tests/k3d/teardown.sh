#!/usr/bin/env bash
set -euo pipefail
CLUSTER="${ARGO_HOLD_K3D_CLUSTER:-argo-hold-test}"
[[ "$CLUSTER" =~ ^argo-hold-[a-z0-9-]+$ ]] || { echo "refusing: cluster name '$CLUSTER' must match ^argo-hold-[a-z0-9-]+$" >&2; exit 1; }
echo "deleting k3d cluster: $CLUSTER (context k3d-${CLUSTER})"
k3d cluster delete "$CLUSTER"

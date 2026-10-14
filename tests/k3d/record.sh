#!/usr/bin/env bash
# Record real kubectl output for the fake kubectl and the fixtures. Re-run to refresh.
set -euo pipefail
CLUSTER="${ARGO_HOLD_K3D_CLUSTER:-argo-hold-test}"
[[ "$CLUSTER" =~ ^argo-hold-[a-z0-9-]+$ ]] || { echo "refusing: cluster name '$CLUSTER' must match ^argo-hold-[a-z0-9-]+$" >&2; exit 1; }
CTX="k3d-${CLUSTER}"
# Refuse to touch anything unless the context points at a local API server.
SERVER="$(kubectl --context "$CTX" config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
HOST="${SERVER#*://}"; HOST="${HOST%%[:/]*}"
case "$HOST" in
  127.0.0.1|localhost|0.0.0.0) ;;
  *) echo "refusing: context $CTX server '${SERVER:-none}' is not local" >&2; exit 1 ;;
esac
echo "target: context $CTX"
mkdir -p "$(dirname "$0")/../fixtures/recorded"
OUT="$(cd "$(dirname "$0")/../fixtures" && pwd)/recorded"
K=(kubectl --context "$CTX")
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

"${K[@]}" -n argo-cd get applications.argoproj.io toy -o json > "$OUT/application.json"
"${K[@]}" -n toy get deployment guestbook-ui -o json > "$OUT/deployment.json"
set +e
"${K[@]}" -n toy get deployment nope -o json > /dev/null 2> "$OUT/notfound.stderr"
echo $? > "$OUT/notfound.exit"
set -e
"${K[@]}" -n toy patch deployment guestbook-ui --type strategic --field-manager argo-hold-rec \
  --dry-run=server -o json \
  -p '{"spec":{"template":{"spec":{"tolerations":[{"key":"k","operator":"Exists"}]}}}}' \
  > "$OUT/dryrun_patch_strategic.json"
echo '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"rec-cm","namespace":"toy"},"data":{"a":"1"}}' \
  | "${K[@]}" apply --server-side --field-manager argo-hold-rec --force-conflicts \
      --dry-run=server -f - -o json > "$OUT/dryrun_apply_new.json"
printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: toy\ndata:\n  a: "1"\n' > "$TMP/cm.yaml"
"${K[@]}" create --dry-run=client -f "$TMP/cm.yaml" -o json > "$OUT/convert_yaml.json"

# Watch stream: start a watch, make two real changes, stop the watch.
"${K[@]}" -n toy get deployment guestbook-ui -w --output-watch-events -o json > "$OUT/watch_stream.txt" 2>/dev/null &
WPID=$!
sleep 2
"${K[@]}" -n toy annotate deployment guestbook-ui argo-hold-rec=1 --overwrite >/dev/null
sleep 1
"${K[@]}" -n toy annotate deployment guestbook-ui argo-hold-rec- >/dev/null
sleep 2
kill "$WPID" 2>/dev/null || true
wait "$WPID" 2>/dev/null || true
echo "recorded to $OUT"

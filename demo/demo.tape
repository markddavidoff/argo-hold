# argo-hold demo tape for charmbracelet/vhs (https://github.com/charmbracelet/vhs).
# Render with `make demo`, which writes demo/demo.gif. Run from the repo root.
#
# The demo runs entirely against a FAKE kubectl (demo/bin/kubectl, a symlink to the test
# suite's fake) and a scratch store. No real cluster is contacted. The Hide block sets that
# up; the Show block is what appears in the GIF.

Output demo/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1200
Set Height 760
Set Padding 24
Set TypingSpeed 40ms

Hide
Type "export PATH=\"$PWD/scripts:$PWD/demo/bin:$PATH\"" Enter
Type "export ARGO_HOLD_KUBECTL=\"$PWD/demo/bin/kubectl\"" Enter
Type "export ARGO_HOLD_STATE_DIR=\"$(mktemp -d)\"" Enter
Type "export FAKE_KUBECTL_STORE=\"$(mktemp -d)/cluster.json\"" Enter
Type "export ARGO_HOLD_TICK=0.2" Enter
Type "python3 demo/setup >/dev/null" Enter
Type "clear" Enter
Show

Type "# argo-hold runs against a FAKE cluster here. No real cluster is touched." Enter
Sleep 2s

Type "# 1. Dry-run: see exactly what the hold would change." Enter
Type "argo-hold start --app web --context prod-west1 \\" Enter
Type "  --target deployment/web -n web --type merge \\" Enter
Type "  --patch '{\"spec\":{\"replicas\":3}}' --dry-run" Enter
Sleep 3s

Type "# 2. Start the hold in the background (auto-sync goes off; the change stays)." Enter
Type "argo-hold start --app web --context prod-west1 \\" Enter
Type "  --target deployment/web -n web --type merge \\" Enter
Type "  --patch '{\"spec\":{\"replicas\":3}}' --detach --yes --interval 2" Enter
Sleep 3s

Type "# 3. Check the hold, then test whatever you came to test." Enter
Type "argo-hold status --app web" Enter
Sleep 3s
Type "argo-hold list" Enter
Sleep 2s

Type "# 4. Release: revert the change and turn auto-sync back on." Enter
Type "argo-hold release --app web" Enter
Sleep 3s
Type "argo-hold list" Enter
Sleep 3s

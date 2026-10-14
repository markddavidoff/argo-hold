# argo-hold

[![CI](https://github.com/markddavidoff/argo-hold/actions/workflows/ci.yml/badge.svg)](https://github.com/markddavidoff/argo-hold/actions/workflows/ci.yml)

`argo-hold` tests an infrastructure change in a live cluster before it merges to GitOps. It
turns one Argo CD app's auto-sync off and keeps a live change applied to that app's resources.
`argo-hold release` reverts the change and restores the app's sync policy. With `--freeze`,
`argo-hold` applies no change: it turns auto-sync off and keeps it off, so nothing syncs to
that app until `release`.

Without a hold, an app with auto-sync and `selfHeal` reverts a live `kubectl patch` within
seconds. `argo-hold` keeps the change in place, then undoes it, and it never edits git.

## Requirements

- Python 3.10 or newer. The tool uses only the standard library.
- `kubectl`, with access to the cluster context you pass in `--context`.
- Argo CD running in namespace `argo-cd`.

## Install

```bash
git clone https://github.com/markddavidoff/argo-hold.git
cd argo-hold
./install.sh                  # symlinks argo-hold into ~/.local/bin
./install.sh --prefix DIR     # or into DIR/bin
./install.sh --uninstall
```

## Quick start

```bash
# 1. Plan only. Prints the context, app, touched paths and release behavior; changes nothing.
argo-hold start --app my-app --context my-ctx \
  --target deployment/web -n web --type merge \
  --patch '{"spec":{"replicas":3}}' --dry-run

# 2. Run it for real. Confirm at the prompt; the holder keeps running in the background.
argo-hold start --app my-app --context my-ctx \
  --target deployment/web -n web --type merge \
  --patch '{"spec":{"replicas":3}}' --detach

# 3. Test whatever you came to test, then undo everything.
argo-hold status --app my-app
argo-hold release --app my-app
```

A hold ends on its own after `--ttl` (default 2h). While it lasts, the app gets no other
GitOps changes, so keep it short. See [Safety](#safety) before pointing this at a shared
cluster.

## How it works

An app with auto-sync on and `selfHeal` reverts a live edit: a `kubectl patch` to a field
that git manages is gone within seconds (about one second on the k3d test cluster). To test a
change live, auto-sync has to stay off and the change has to stay applied until the test ends.
`argo-hold` does both with a **holder** process, and `release` undoes them.

### What it changes in the cluster

| Object | Change at `start` | Change at `release` |
|---|---|---|
| The Argo CD Application | Sets `spec.syncPolicy.automated` to null (only when it was set). The other `syncPolicy` fields stay. | Puts the original `automated` value back, then sets the annotation `argocd.argoproj.io/refresh=hard` (only when the original had `automated`). |
| Each target object | Applies your change: a `kubectl patch`, or for `-f` a create (new object) or a merge patch (existing object). A freeze has no target objects. | Reverts the touched paths with a JSON patch, or deletes an object the hold created. |

`kubectl` records `argo-hold` as the field manager on these writes. The tool changes nothing
else: no other Application field, no other object, no Argo CD setting, no git repository.

### `start`

`start` runs these steps in order:

1. **Check the request.** It refuses a denylisted app, a `Secret` target, and an app that
   already has a hold.
2. **Plan, read-only.** It reads the Application and each target and asks the API server to
   dry-run the change (`--dry-run=server`). The difference between the live object and the
   dry-run result is the list of **touched paths**. An added block is one path, and an array
   is always one whole path.
3. **Show the plan** and ask `Proceed? [y/N]` unless `--yes` is set. `--dry-run` stops here.
4. **Take the lock and plan again.** Creating `<app>.json` in the state directory is the
   one-hold-per-app lock. If the second plan differs from the one you confirmed, `start`
   removes the file and exits.
5. **Turn auto-sync off.**
6. **Wait for a running sync** to finish, for at most `--wait-op` (default 5m). It never ends
   a sync; on timeout it exits and applies nothing.
7. **Apply the change** and hand over to the holder. A freeze has no change to apply.

A failure after step 5 runs the release path before `start` exits.

### While the change is held

After `start` applies the change, the holder keeps auto-sync off and the change applied. It
has three parts:

- **Watchers.** One `kubectl get -w` per watched object: the Application and each target.
- **Reactor.** One thread reads the watch events, so every write to the cluster is serial.
  When `spec.syncPolicy.automated` reappears on the Application, the reactor removes it again
  and logs a `WARNING`. When a touched path on a target no longer matches its held value
  (Argo or someone else reverted it), the reactor re-applies the change and logs it. It reads
  the paths from the event itself, so a check costs no extra API call. It defers more than 5
  re-applies in 60 seconds to the next resync.
- **Timer.** Every `--interval` seconds (default 60) it writes a heartbeat and queues a full
  re-read of every watched object, which catches a watch that stalled silently. It also ends
  the hold when the TTL (`--ttl`, default 2h) runs out. The timer never changes the cluster.

The holder reacts to an event within seconds. `--interval` bounds only the worst case of a
silent watch stall.

### `release`

`release` runs one revert path, whether the holder runs it (TTL expiry, Ctrl-C, SIGTERM, or
`release` signalling a live holder) or `release` runs it from the state file because the
holder is gone. The order is targets, then the Application policy, then the hard refresh. A
failing step does not stop the next one, because leaving auto-sync off is worse than a
half-reverted change.

- **The revert never relies on Argo.** On the k3d cluster, Argo left a hand-added field in
  place after the policy was restored. The tool reverts each touched path itself.
- **It never overwrites someone else's change.** Before it restores a path, the tool checks
  that the path still holds the value the hold set. If another writer changed it, the tool
  skips the path with a warning and keeps the state file; `release --force` overwrites it.
- **A created object is deleted** only when `argo-hold` is its only field manager, or when
  `--force` is set.

### Freeze: keep auto-sync off, apply nothing

`start --freeze` runs the same sequence without a change. It turns `automated` off (only when
it was set), records the original value, waits for a running sync, and starts the holder. The
holder watches only the Application, and turns `automated` off again if it reappears. Use it
to stop an app from syncing while you do something else, for example to freeze a parent app
such as `app-root` while another hold runs a test on a child.

`--freeze` is an alternative to `--patch`, `--patch-file` and `-f`, and takes no `--target`,
`-n` or `--type`.

**Releasing a freeze syncs everything that is pending.** `release` restores the original
`automated` value and requests a hard refresh, exactly as for any other hold. With auto-sync
back on, Argo syncs every change that merged to git during the freeze, all at once. The
`--dry-run` plan says so (`release:` line), and `release` prints a `NOTICE:` line before it
acts; the holder writes the same line to the log when the TTL runs out or a signal ends the
hold. If `automated` was already off when the freeze started, release leaves it off and the
notice says so. A freeze cannot undo a sync that lands in the moment between something
re-enabling `automated` and the holder turning it off again.

### Files on disk

Each hold has `<app>.json` and `<app>.log` in `$ARGO_HOLD_STATE_DIR`, or
`~/.local/state/argo-hold` when that is unset (directory mode 0700, files 0600). The JSON file
records the context, TTL, the original `automated` value, the change (`change.kind` is
`patch`, `manifest` or `freeze`), and each target's touched paths with their before and held
values. A freeze has no targets. `status` and `release` need only this file,
so both work after the holder crashed.

[`docs/how-it-works.md`](docs/how-it-works.md) has the details: failure modes, what is
unverified, and the k3d findings behind these rules.

## A session, step by step

This is real output from the fake `kubectl` that the unit tests use, so the context is
`test-ctx`. The code path is the same against a real cluster; only the context name and the
object contents differ.

```text
$ argo-hold start --app otel-collector --context test-ctx --target daemonset/otel-collector-logs \
    -n monitoring --type strategic --patch '{...affinity...}' --dry-run
context:   test-ctx
app:       otel-collector (namespace argo-cd)
ttl:       2h    interval: 60s
on-call:   #on-call (post the on-call notice here before a live start)
auto-sync: will be turned OFF
target:    daemonset/otel-collector-logs -n monitoring
             add      /spec/template/spec/affinity  (object(1 keys))
dry-run: nothing was changed

$ argo-hold start ... --detach --yes          # same command, now for real
(prints the plan again, then:)
argo-hold: holding otel-collector (pid 63056); log: ~/.local/state/argo-hold/otel-collector.log

$ argo-hold status --app otel-collector
app:         otel-collector   context: test-ctx
holder:      pid 63056 (live), phase holding
heartbeat:   1s ago (interval 60s)
ttl left:    1h 59m
activity:    0 auto-sync re-disable(s), 0 re-apply(s)
live:        auto-sync off, sync status Synced

$ argo-hold list
otel-collector                 test-ctx                                 live      ttl 1h 59m

$ argo-hold start ... --yes                   # a second hold on the same app
argo-hold: a hold for otel-collector already exists; use `argo-hold status --app otel-collector` or `argo-hold release --app otel-collector`
                                              # exit 1

$ argo-hold release --app otel-collector
releasing otel-collector on context test-ctx
released

$ argo-hold list
no active holds
```

The log of that session, from `~/.local/state/argo-hold/otel-collector.log`:

```text
holding otel-collector on test-ctx; ttl 2h; release with: argo-hold release --app otel-collector
releasing (signal SIGTERM)
daemonset/otel-collector-logs: reverted 2 op(s)
otel-collector: restored spec.syncPolicy.automated
otel-collector: requested hard refresh
released
```

Each log line starts with a timestamp, which this example omits. The log shows paths and
counts, never values.

### A freeze, step by step

Real output from the same fake `kubectl`. The app is `app-root`, which is on the denylist, so
`--allow-sensitive` is required.

```text
$ argo-hold start --app app-root --context test-ctx --freeze --allow-sensitive --dry-run
context:   test-ctx
app:       app-root (namespace argo-cd)
ttl:       2h    interval: 60s
on-call:   #on-call (post the on-call notice here before a live start)
mode:      freeze (no change is applied; only auto-sync is turned off)
auto-sync: will be turned OFF
release:   turns auto-sync back on and requests a hard refresh; EVERY change pending in git for app-root syncs at once
dry-run: nothing was changed

$ argo-hold status --app app-root
app:         app-root   context: test-ctx
mode:        freeze (no change)
holder:      pid 77584 (live), phase holding
heartbeat:   0s ago (interval 1s)
ttl left:    1h 59m
activity:    0 auto-sync re-disable(s)
live:        auto-sync off, sync status Synced

$ argo-hold list
app-root                       test-ctx                                 live      ttl 1h 59m  freeze (no change)

$ argo-hold release --app app-root
releasing app-root on context test-ctx
NOTICE: releasing the freeze on app-root turns auto-sync back on, and a hard refresh is requested: every change pending in git syncs at once
released
```

Two holds on different apps run at the same time, for example a freeze on `app-root` and a
patch hold on `otel-collector`. Each has its own state file, holder and TTL. Release them in
either order; releasing one leaves the other held.

## Usage

```bash
argo-hold start  --app NAME --context CTX \
                 --target KIND/NAME -n NS (--patch JSON | --patch-file FILE) --type merge|json|strategic
argo-hold start  --app NAME --context CTX -f MANIFEST
argo-hold start  --app NAME --context CTX --freeze     # no change; only keep auto-sync off
argo-hold status --app NAME
argo-hold release --app NAME [--force]
argo-hold list
```

A typical session:

```bash
argo-hold start ... --dry-run          # print the plan; change nothing
# post the on-call notice in the channel on the plan's on-call: line (required; see Safety)
argo-hold start ... --detach           # confirm at the prompt; the holder runs in the background
argo-hold status --app NAME            # holder state, TTL left, live auto-sync state
argo-hold release --app NAME           # revert the change, restore the app
# reply in the on-call thread that the hold is released
```

### `start` flags

| Flag | Meaning |
|---|---|
| `--app APP` | The Argo CD Application to hold. Required. |
| `--context CONTEXT` | The kube context. Required; printed before anything changes. |
| `-n`, `--namespace NS` | Namespace of the target objects. |
| `--target KIND/NAME` | An object to change. Repeat for more than one. |
| `--patch JSON` | Patch body as JSON. |
| `--patch-file FILE` | File that holds the patch body as JSON. |
| `-f`, `--filename FILE` | A manifest. Targets come from the manifest, so `--target` and `--type` are rejected. An object that does not exist is created; one that exists is merge-patched. |
| `--freeze` | Apply no change: only turn auto-sync off and keep it off. Rejects `--target`, `-n` and `--type`. `release` turns auto-sync back on, and every change pending in git syncs at once. |
| `--type merge\|json\|strategic` | Patch type. Required with `--patch` and `--patch-file`. |
| `--ttl 2h` | Time until the hold releases itself. Accepts `s`, `m`, `h` suffixes. Default `2h`. |
| `--interval 60` | Heartbeat and safety-resync period in seconds. Default `60`. |
| `--wait-op 5m` | How long to wait for a running sync before giving up. Default `5m`. |
| `--dry-run` | Print the plan and stop. Nothing is changed. |
| `--detach` | Run the holder in the background after you confirm. |
| `--yes` | Skip the confirmation prompt. |
| `--allow-sensitive` | Allow the denylisted apps. |

Exactly one of `--patch`, `--patch-file`, `-f`, and `--freeze` is required.

### `release`

`release` signals a live holder and waits for it to revert. If the holder is gone, `release`
reverts from the state file. A field that someone else changed during the hold is skipped
with a warning; `--force` overwrites it. Releasing a freeze prints a `NOTICE:` line first:
auto-sync comes back on and every pending change syncs at once.

### Environment

| Variable | Default | Effect |
|---|---|---|
| `ARGO_HOLD_STATE_DIR` | `$HOME/.local/state/argo-hold` | Where state and log files live |
| `ARGO_HOLD_KUBECTL` | `kubectl` | The `kubectl` binary to run; the tests point it at a fake |
| `ARGO_HOLD_DENYLIST_EXTRA` | unset | Comma- or space-separated app names to refuse in addition to the built-in denylist (`argo-cd`, `app-root`, `external-secrets`). `--allow-sensitive` overrides them |
| `ARGO_HOLD_ONCALL_CHANNEL` | `#on-call` | The on-call channel that `start` names on the `on-call:` line of its plan output. A blank value uses the default. The tool never posts to it |

## Agent prompts

[`docs/agent-prompts.md`](docs/agent-prompts.md) has two prompts that let a coding agent run a
whole hold cycle: dry-run, on-call notice, apply, observe, release. They need `kubectl` access
to the context and `argo-hold` on the agent's `PATH`.

## Safety

- `--context` is required and printed with the app and targets before anything changes.
  `start` asks for confirmation unless `--yes` is set.
- One hold per app. A second `start` for the same app is refused.
- A hold ends on its own after `--ttl` (default 2h).
- Every live hold is announced in the on-call channel before it starts, and closed out in the
  same thread after `release`. The `on-call:` line of the plan output names the channel
  (default `#on-call`; set `ARGO_HOLD_ONCALL_CHANNEL` to change it). Both
  [agent prompts](docs/agent-prompts.md) require the post. The tool does not post to Slack and does not check that a post
  exists.
- `argo-cd`, `app-root`, and `external-secrets` are refused without `--allow-sensitive`, as is any
  app named in `ARGO_HOLD_DENYLIST_EXTRA`. This includes `--freeze`.
- Targets of kind `Secret` are refused.
- Logs, `--dry-run` output, and `status` show paths, operations, and value shapes, never values.
  The state file (mode 0600) holds the values at the touched paths. Patch bodies appear in
  the `kubectl` command line while a command runs.
- Writes use `kubectl patch` or server-side apply with field manager `argo-hold`. The tool
  never runs client-side `kubectl apply` on Argo-managed objects.
- The tests never touch a production cluster. The unit tests use a fake `kubectl`; the opt-in integration test uses a throwaway local k3d cluster.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error or refusal |
| 2 | Usage error |
| 3 | Finished with warnings; the state file is kept (`release --force` finishes the job), or `status` found a dead or stale holder |

## Tests

```bash
make test       # pytest suite on a fake kubectl; no cluster needed
make test-k3d   # opt-in: throwaway local k3d with Argo CD; needs docker, k3d, network
```

## License

[MIT](LICENSE).

## More

- [`docs/agent-prompts.md`](docs/agent-prompts.md): prompts that let an agent run a hold.
- [`docs/how-it-works.md`](docs/how-it-works.md): start sequence, the holder, the revert,
  failure modes, and what is unverified.
- [`docs/k3d-findings.md`](docs/k3d-findings.md): what the k3d runs observed.

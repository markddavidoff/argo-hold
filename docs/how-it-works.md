# How argo-hold works

`argo-hold` keeps a live change applied to one Argo CD-managed app while auto-sync is
off, so an engineer can test the change in the cluster before it merges to GitOps.
`release` reverts the change and restores the app's sync policy. `start --freeze` is the
same tool with no change: it only holds auto-sync off ([Freeze mode](#freeze-mode)). For
commands and flags, see [`../README.md`](../README.md).

The tool talks to the cluster only through `kubectl`; it never uses the `argocd` CLI.
Argo CD is expected in namespace `argo-cd` by default; pass `--argocd-namespace` (or set
`ARGO_HOLD_ARGOCD_NAMESPACE`) for a different install, such as the stock `argocd` namespace.

## What it changes in the cluster

| Object | Change at `start` | Change at `release` |
|---|---|---|
| The Argo CD Application | Sets `spec.syncPolicy.automated` to null (only when it was set). The other `syncPolicy` fields stay. | Puts the original `automated` value back, then sets the annotation `argocd.argoproj.io/refresh=hard` (only when the original had `automated`). |
| Each target object | Applies your change: a `kubectl patch`, or for `-f` a create (new object) or a merge patch (existing object). A freeze has no target objects. | Reverts the touched paths with a JSON patch, or deletes an object the hold created. |

`kubectl` records `argo-hold` as the field manager on these writes. The tool changes nothing
else: no other Application field, no other object, no Argo CD setting, no Git repository.

## Start sequence

`start` runs these steps in order:

1. **Check the request.** `start` validates the app name, refuses the denylisted apps
   (`argo-cd`, `app-root`, `external-secrets`, plus anything in `ARGO_HOLD_DENYLIST_EXTRA`) unless
   `--allow-sensitive` is set, refuses `Secret` targets, and refuses when a hold for the
   app already exists.
2. **Plan, read-only.** `start` reads the Application and each target, and asks the API
   server to dry-run the change (`--dry-run=server`). The difference between the live
   object and the dry-run result gives the touched paths (next section).
3. **Show the plan.** `start` prints the context, app, on-call channel, targets, and touched
   paths, then asks `Proceed? [y/N]` unless `--yes` is set. `--dry-run` stops here.
4. **Take the lock and re-plan.** `start` creates the state file, which is the
   one-hold-per-app lock, then plans again. If the new plan differs from the one the
   user confirmed, `start` removes the state file and exits with "the cluster changed
   since the plan was shown".
5. **Turn auto-sync off.** If `spec.syncPolicy.automated` is set, `start` patches it to
   null. Other `syncPolicy` fields stay as they are.
6. **Wait for a running sync.** `start` polls `status.operationState.phase` every 5
   seconds until it is not `Running`, for at most `--wait-op` (default 5m). On timeout
   it exits without applying anything. It never terminates a sync.
7. **Apply the change** and hand over to the holder. With `--freeze` there is no change to
   apply.

A failure after step 5 runs the release path before exit. If that release has warnings
or errors, the state file stays so `argo-hold release` can finish the job.

## The holder

After `start` applies the change, the holder process keeps it applied. It has three
parts: watchers, one reactor, and a timer.

**Watchers.** The holder starts one `kubectl get <object> -w --output-watch-events -o json`
per watched object: the Application and each target. Each runs in its own thread and
pushes parsed events onto one queue. A watcher whose `kubectl` exits restarts with a
backoff of 1, 2, 5, 10, then 30 seconds, and asks the reactor for a full re-read.

**Reactor.** One thread drains the queue, so all writes to the cluster are serial.

- On an Application event, if `spec.syncPolicy.automated` has reappeared, the reactor
  patches it to null again and logs a `WARNING` line.
- On a target event, the reactor compares each touched path in the event's own object
  with its held value. On a difference it re-applies the change and logs `drift on
  <target>; re-applying`. This costs no extra API call.
- Events caused by the reactor's own writes find the state already correct, so they do
  nothing.
- More than 5 re-applies inside 60 seconds are deferred to the next resync and logged.
  When a controller keeps reverting the change, the reactor logs it and stops re-applying until the next resync.
- If a patched target is deleted, the reactor logs a warning and cannot re-apply the
  patch to a missing object. A target the hold created from `-f` is created again;
  a deleted pre-existing `-f` target cannot be re-applied (logged as an error).

**Timer.** The timer never corrects anything. Every `--interval` seconds (default 60) it
writes the heartbeat and queues a full resync, which re-reads the Application and each
target. It also ends the hold when the TTL expires.

The timer exists because a stalled `kubectl get -w` is silent: the connection stays
open and no event arrives. Without the resync, a re-enable or a drift behind a stalled
watch would go unseen indefinitely. With it, the worst case is one `--interval`.

The reaction to a normal event takes seconds, not `--interval`. On the k3d cluster, one
sample showed Argo reverting a git-managed field about one second after auto-sync was
re-enabled ([findings](k3d-findings.md), section 2), so the holder cannot wait for a
timer to protect the change.

**Foreground and detached.** `start` stays in the foreground by default; Ctrl-C releases.
With `--detach`, `start` asks for confirmation, then re-executes itself in the
background with `--yes` and returns once the state file shows the holder is `holding`.
The log is `<app>.log` in the state directory.

## Touched paths and why a dry-run diff

The tool does not reimplement patch semantics. It sends the change to the API server
with `--dry-run=server -o json` and diffs the result against the live object. The diff
ignores `status` and the volatile metadata fields (`managedFields`, `resourceVersion`,
`generation`, `uid`, `creationTimestamp`, `selfLink`). Each leaf that differs is a
touched path with a `before` value and a `held` value.

- A key that did not exist is recorded at its topmost missing key, so an added
  `affinity` block is one path, and reverting it removes the whole block.
- An array is always one whole path, so reverting restores the entire array.
- A target created by `-f` is recorded as not existing before, and reverting deletes it.

Reimplementing patch semantics in Python would be wrong for strategic-merge lists.
Replacing the whole object from a snapshot would overwrite unrelated changes that
other writers made during the hold.

## Writes

- Patches run as `kubectl patch --type merge|json|strategic --field-manager argo-hold`.
- A `-f` manifest for an object that does not exist is created with
  `kubectl apply --server-side --field-manager argo-hold --force-conflicts`.
- A `-f` manifest for an object that exists is sent as a merge patch. On k3d, a partial
  server-side apply onto an Argo-managed (client-side applied) object worked once and
  then failed on the identical second apply ([findings](k3d-findings.md), section 4).
- The tool never runs client-side `kubectl apply` on Argo-managed objects, because that
  rewrites `last-applied-configuration`.

## Release and the no-clobber rule

`release` runs one revert path, in this order: targets, then the Application policy,
then a refresh. The path is the same whether the holder runs it (TTL expiry, SIGINT,
SIGTERM, or `release` signalling a live holder) or `release` runs it from the state file
because the holder is gone. A failing step does not stop the next: leaving auto-sync off
is worse than a half-reverted change.

**Targets that existed.** The tool builds one JSON patch. For each touched path it adds a
`test` operation on the held value, then a `replace`, `add`, or `remove` that restores the
`before` value. The `test` makes the revert check-and-set: if another writer changed the
path after the last read, the patch fails and the tool re-reads once and retries. A path
whose live value matches neither the held value nor the `before` value was changed by
someone else. The tool logs a warning, skips that path, and leaves it for `--force`.

**Targets the hold created.** The tool deletes the object only if `argo-hold` is its only
field manager, or `--force` is set.

**Application policy.** The tool writes only `spec.syncPolicy.automated`.

| Live value | Action |
|---|---|
| Absent | Set it to the original value |
| Equal to the original | Nothing |
| Anything else | Warning, left as is; `--force` sets it back to the original (or removes it, if the original had none) |

**Refresh.** If the original policy had `automated` and the policy step produced no
warning, the tool sets the annotation `argocd.argoproj.io/refresh=hard`. A skipped target
field does not suppress it.

Release never relies on Argo to undo the change. On k3d, after the policy was restored,
Argo left hand-added fields in place and the app stayed `Synced`
([findings](k3d-findings.md), section 1).

When a release skips a field or a step fails, the state file stays and the next
`argo-hold release` retries; `--force` overwrites fields changed by someone else. The exit
code depends on who ran the revert:

- When `release` runs the revert itself (the holder is gone, or `--force` after a holder
  released with a kept state file), it exits 1 if any step failed, else 3 if a field was
  skipped, else 0. Errors take precedence over skipped fields.
- When no hold is recorded for the app, `release` prints that and exits 0.
- When a live holder runs the revert, `release` waits for it to exit. It exits 0 if the
  holder removed the state file. If the state file remains, `release` exits 3 ("released
  with warnings"), whether the holder's release had warnings or errors; read the log.
  It exits 1 if the holder does not exit within 120 seconds.

## Freeze mode

`start --freeze` holds auto-sync off for one Application and applies nothing. It exists for
freezing an app so no deploy syncs while something else happens, for example a parent
app-of-apps such as `app-root` while another hold tests a change on a child. Before it, the
only way was a no-op patch with a dummy target.

**What differs from a patch hold**

- `--freeze` is one of four mutually exclusive change modes (`--patch`, `--patch-file`, `-f`,
  `--freeze`); exactly one is required. It rejects `--target`, `-n` and `--type` with a
  `SafetyError` (exit 1).
- The state file records `change: {"kind": "freeze"}` and `targets: []`. Nothing else in the
  schema changed, so state files from before `--freeze` still load (a missing `change.kind`
  reads as a patch hold), and a freeze state file also works with a build that predates
  `--freeze`.
- The plan has no `target:` lines. It adds a `mode:` line and a `release:` line that say what
  release will do.
- The holder has one watcher, on the Application. The reactor turns `automated` off again if it
  reappears, as for any hold. The resync re-reads only the Application.
- `status` shows `mode: freeze (no change)` and the re-disable count, without the re-apply
  count. `list` appends `freeze (no change)` to the line. Output for patch and manifest holds
  is unchanged.

**What stays the same.** The denylist applies (freezing `app-root` needs `--allow-sensitive`),
as do the `on-call:` line, the one-hold-per-app lock, the TTL release, the `--wait-op` wait
for a running sync, the echo of `--context`, and the rule that logs hold no values. Holds on
different apps are independent: each has its own state file, holder and TTL, and releasing one
in either order leaves the other held (covered by `tests/test_freeze.py`).

**Release.** The revert path is the same as for any hold. With no targets, it restores
`spec.syncPolicy.automated` and, when the original had `automated` and the restore produced no
warning, requests a hard refresh. The consequence is specific to a freeze: with auto-sync back
on, Argo syncs everything that merged to git while the app was frozen, all at once. For a
parent app that is every pending change to every child it defines. The tool says so in three
places:

- the `release:` line of the `--dry-run` plan and the confirmation;
- a `NOTICE:` line that `release` prints before it acts (or logs, when the revert runs in the
  holder: TTL expiry, SIGINT, SIGTERM);
- the same `NOTICE:` in `<app>.log`.

If `automated` was off before the freeze, the notice says release leaves it off and nothing is
restored or refreshed.

The hard refresh is kept for a freeze, as it is for every other hold. Restoring `automated` is
what lets Argo sync the pending changes. On k3d, one manual run restored `automated` with no
refresh annotation and Argo synced a pending drift in 1.1 s ([findings](k3d-findings.md),
section 5). So the refresh is not the trigger; it clears the manifest cache and forces a fresh
comparison, so an operator who releases while watching sees a current result. A `--no-refresh`
option would not prevent the sync, so the tool has none. Its cost is repo-server load: a hard
refresh of an app-of-apps re-renders it, and its size on a real `app-root` is unmeasured.

## State file and locking

Each hold has two files in the state directory: `<app>.json` and `<app>.log`. The
directory is `$ARGO_HOLD_STATE_DIR`, or `$HOME/.local/state/argo-hold` when that is unset.
The directory has mode 0700 and the files 0600.

- Creating the JSON file is the one-hold-per-app lock. The create fails if the file
  exists, and `start` refuses. The tool publishes the state file create-exclusive (`scripts/ah_lock.py`) for this.
- The holder rewrites the file on each heartbeat. A holder is `dead-pid` when its pid is
  gone and `stale` when its heartbeat is older than 2.5 times `--interval`.
- The file records the app, context, TTL and expiry, interval, the original
  `spec.syncPolicy.automated`, the change body (the patch or the manifest; for a freeze,
  only `{"kind": "freeze"}`), and for each target the touched paths with their `before` and
  `held` values. A freeze has no targets.
- The file holds values at the touched paths, so it can contain sensitive data. That is
  why the tool refuses `Secret` targets. Logs, `--dry-run`, and `status` print paths,
  operations, and value shapes (`object(1 keys)`, `array(3)`), never values.

`status` and `release` work from this file alone, so they still work after the holder
crashed.

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| Holder killed (`kill -9`, reboot) | Auto-sync stays off, the change stays applied, the heartbeat goes stale | `argo-hold status` warns; `argo-hold release --app X` works from the state file |
| Orphaned `kubectl get -w` children after a `kill -9` of the holder | The children keep running after the holder is gone | They exit on their next write; no action needed |
| Watch stalls silently | A re-enable or drift goes unseen until the next resync | Bounded by `--interval` |
| Something re-enables `automated` | Argo may revert the change within seconds | The reactor turns it off again and re-applies at once; loud log line |
| Someone else edits a held field | Release would overwrite their change | The field is skipped with a warning; `--force` overrides |
| A `--patch` target is deleted | The holder cannot re-apply a patch to a missing object | The holder logs a warning; stop the hold with `release` and start again once the object exists |
| TTL expires | The hold ends on its own | The timer starts the normal release path. For a freeze, this syncs every pending change |
| Freeze holder killed (`kill -9`, reboot) | Auto-sync stays off and nothing syncs to the app; the heartbeat goes stale | `argo-hold status` warns; `argo-hold release --app X` restores `automated` and everything pending syncs |
| Something re-enables `automated` on a frozen app | Argo can sync pending changes before the holder turns auto-sync off again. The holder reacts within seconds and cannot undo a sync | Keep the freeze short; read `status` for the re-disable count. The race window is unmeasured |
| A frozen app is released | Argo syncs every change that merged to git during the freeze, at once | Read the `NOTICE:` line; post the on-call reply when it is done |
| Argo sync is Running at start | Applying mid-sync races it | `start` waits up to `--wait-op`, then exits with nothing applied |
| SIGINT or SIGTERM while `start` is still in `begin` (for example waiting up to `--wait-op` for a Running sync) | The signal handlers are installed only after `begin` returns. SIGINT raises `KeyboardInterrupt`, which makes `begin` roll back. SIGTERM (or SIGKILL) ends the process at once and leaves a partial hold | After SIGINT, the rollback runs. After SIGTERM or SIGKILL, `argo-hold release --app X` finishes the job from the state file |

## Worked example

The example target is app `otel-collector`, `daemonset/otel-collector-logs` in namespace
`monitoring`: add a node affinity that excludes the `infra-pool` node pool.

**The live command below was not run against a production cluster.** Only the dry-run was run,
against the fake `kubectl` that the unit tests use. The effect described for a live cluster
is an expectation, not an observation.

Dry-run against the fake cluster (`ARGO_HOLD_KUBECTL` points at
`tests/fakes/kubectl`, `FAKE_KUBECTL_STORE` at a seeded store). The context
`test-ctx` is the fake's name:

```
context:   test-ctx
app:       otel-collector (namespace argo-cd)
ttl:       2h    interval: 60s
on-call:   #on-call (post the on-call notice here before a live start)
auto-sync: will be turned OFF
target:    daemonset/otel-collector-logs -n monitoring
             add      /spec/template/spec/affinity  (object(1 keys))
dry-run: nothing was changed
```

The command to run against a live cluster:

```bash
argo-hold start --app otel-collector \
  --context prod-west1 \
  --target daemonset/otel-collector-logs -n monitoring --type strategic \
  --patch '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"gke-nodepool","operator":"NotIn","values":["infra-pool"]}]}]}}}}}}}' \
  --dry-run          # then drop --dry-run, add --detach, confirm at the prompt
# expected while held: 12 pods become 9; `argo-hold status --app otel-collector`
argo-hold release --app otel-collector   # restores everything and {prune: true, selfHeal: true}
```

Expected while held: the DaemonSet's pod count drops from 12 to 9. After `release`, the
affinity block is removed and the policy returns to `{prune: true, selfHeal: true}`.
Neither has been observed.

## Limitations and observations

These come from the build and the k3d runs ([findings](k3d-findings.md)).

- **Status counters lag.** `argo-hold status` shows activity counters that the holder
  persists only on the heartbeat tick, so they can lag by up to one `--interval`.
- **Repeated reactions on k3d.** On the k3d Argo CD, a single manual auto-sync re-enable
  produced 4 to 5 re-disables and 4 re-applies before settling. The cause was not traced.
  The final state was correct every time.
- **Strategic-merge lists untested.** Strategic-merge behavior for lists keyed by name
  (`containers`, `env`) was not exercised against real `kubectl`. Only adding
  `tolerations` where none existed was.
- **Revert of a removed key has no guard.** When the change removed a key, `release`
  restores it with a JSON-patch `add` and no `test` operation, because JSON patch cannot
  test for absence. A writer who re-adds the key in that instant is overwritten.
- **Cross-host release.** `release` for a hold recorded by a different host reverts
  locally against the stored context and cannot detect a live remote holder.
- **`ignoreDifferences` unverified.** The behavior of `ignoreDifferences` with
  `RespectIgnoreDifferences` on the app-of-apps was not verified.
  Verifying it needs a git-backed app-of-apps, which the local k3d harness does not have.
- **Orphaned watchers.** After a `kill -9` of the holder, its `kubectl get -w` children
  exit on their next write.
- **Running syncs.** `operation: null` does not end a running sync; it left the app at
  `Running` on k3d ([findings](k3d-findings.md), section 3). The holder only waits for a
  sync to finish.

## What re-enables auto-sync

Something outside `argo-hold` can turn `spec.syncPolicy.automated` back on during a hold: another
tool, a parent app that re-renders the Application, or a person. The holder defends against a
re-enable regardless of its source: it turns auto-sync off again on the watch event and logs each
occurrence. Count them with `argo-hold status --app NAME` (`auto-sync re-disable(s)`).

## Unverified

- `ignoreDifferences` plus `RespectIgnoreDifferences` behavior (above).
- Strategic-merge lists keyed by name, against real `kubectl` (above).
- The worked example against a live cluster, including the pod count change.
- How long Argo takes to revert a git-managed field on a busy cluster, and holder
  behavior when a sync is already in flight at the moment of a re-disable
  ([findings](k3d-findings.md), section 2 and the integration results).
- The cause of the repeated re-disables and re-applies in one k3d run (above).
- Freeze against a real parent app (an app-of-apps root). Nothing was run against one. On k3d
  only a single app was frozen, with a live drift as the pending change; a new git commit and
  a git-backed app-of-apps were not exercised.
- Whether a hard refresh on a frozen parent propagates to its child Applications, and how much
  repo-server load it adds on a large repo.
- How often a sync slips in between something re-enabling `automated` on a frozen app and the
  holder turning it off again. The k3d freeze test re-enabled with no drift present.
- Freeze plus a patch hold on different apps was tested only against the fake `kubectl`
  (`tests/test_freeze.py`), not on k3d (the harness has one app).

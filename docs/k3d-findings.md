# k3d + Argo CD findings

Observed on 2026-10-08 against a throwaway k3d cluster (`k3d-argo-hold-test`, k3d v5.9.0,
k3s v1.35.5, Argo CD v2.12.4 installed into namespace `argo-cd`, client kubectl v1.33.9).
Toy app `toy` (argocd-example-apps `guestbook`, `automated {prune, selfHeal}`) manages
`deployment/guestbook-ui` in namespace `toy`. Observed results (command output) are
stated as such; statements marked "Interpretation" are explanations of the observed
behavior, not separately tested. Experiment 3 also used a second app, defined in
`tests/k3d/toy-hooks-app.yaml`. The cluster was deleted afterwards.
Timestamps in the cluster are UTC; local time was 2026-10-08 evening, so the cluster
shows 2026-10-09 for the same run.

## Recording format findings (affect Task 3 and the fake kubectl)

1. **Watch stream is NDJSON, not indented JSON.** `kubectl get -w --output-watch-events -o json`
   (client v1.33.9) emits one compact JSON document per line, each
   `{"type":"ADDED|MODIFIED|...","object":{...}}`. The brief expected indented documents
   back to back. `ah_kube.iter_json_docs` should accept both (a streaming
   `json.JSONDecoder.raw_decode` loop handles either); the fake must emit the format
   recorded in `watch_stream.txt`. First event is `ADDED` (the current object). The
   recording holds 5 events: `ADDED` plus 4 `MODIFIED` (two from the annotate/un-annotate,
   two from k3s status-only updates that bump `observedGeneration`). Status-only
   events therefore arrive and must not be treated as drift by themselves.
2. **`notfound.stderr`** is exactly `Error from server (NotFound): deployments.apps "nope" not found`; `notfound.exit` is `1`.
3. **`dryrun_patch_strategic.json`** is the full patched object (tolerations present). The
   `-o json` output of a dry-run patch has no `managedFields`.
4. **`dryrun_apply_new.json`** is the object as it would be created, with
   `creationTimestamp` and a `uid` filled in (dry-run assigns them).
5. **`convert_yaml.json`** (`create --dry-run=client -f x.yaml -o json`) is indented JSON
   with four-space indentation and sorted keys; it is a pretty-printed document, not
   compact like the watch stream.
6. Argo CD applies with client-side apply: live objects carry
   `kubectl.kubernetes.io/last-applied-configuration` and an `argocd-controller`
   manager with operation `Update`.

## 1. selfHeal after restore (2026-10-08)

- Command: `patch app toy -n argo-cd --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'`;
  then `patch deployment guestbook-ui --type strategic` adding annotation `hold-test: "1"`
  and toleration `{key: k, operator: Exists}`; then restore
  `automated {prune: true, selfHeal: true}`; wait 90 s.
- Observed: while auto-sync was off the app stayed `Synced`. After restore plus 90 s,
  annotation and toleration were both still on the Deployment, app `Synced`, and
  `operationState.finishedAt` was unchanged (no new sync ran).
- Conclusion: Argo did not remove the fields that were added by hand and are absent from
  git. Interpretation: its diff ignores live-only fields, which would also explain why
  the app stayed `Synced`. The spec's rule stands: release must revert
  the change itself and cannot rely on Argo to undo it. A change that only adds fields
  is invisible to the app's sync status.

## 2. Re-enable race (2026-10-08)

- Command: with auto-sync restored, `patch deployment guestbook-ui --type merge -p '{"spec":{"replicas":2}}'`
  (a field that exists in git), polling `.spec.replicas` once per second.
- Observed: reverted to 1 after 1 second; `operationState.operation.initiatedBy` was
  `{"automated":true}`. The earlier hand-added annotation and toleration were again left
  alone by that sync.
- Conclusion: in this one sample (one drift event, 1 s polling, idle single-app
  cluster), a git-managed field was reverted within about 1 s. This does not
  generalize to a busy cluster or to other resource kinds: the revert latency there is
  unmeasured. What it does show is that the delay can be as short as seconds, so
  re-enabling `automated` while a change to a git-managed field is applied can lose the
  change almost at once, and the reactor should not count on the `--interval` for
  timing.

## 3. Running operation (2026-10-08)

- Command: `patch app toy --type merge -p '{"operation":{"sync":{}}}'`, polling
  `status.operationState.phase` and `.operation`.
- Observed (toy): phase `Running` with `.operation={"sync":{}}`, then `Succeeded` about 2 s
  later with `.operation` cleared by the controller. A plain sync is too short to
  catch mid-flight for termination tests, so a second app `toy-hooks`
  (manifest: `tests/k3d/toy-hooks-app.yaml`, applied with `kubectl apply -f`;
  source is `pre-post-sync` in argocd-example-apps, which has a PreSync Job that ran
  about 18 s) was used.
  (A manual sync needs `syncOptions: ["CreateNamespace=true"]` in the operation or it
  fails with `namespaces "toy-hooks" not found`.)
- Observed, `patch app toy-hooks --type merge -p '{"operation":null}'` while `Running`:
  `.operation` disappears but `status.operationState.phase` stays `Running` with message
  `waiting for completion of hook batch/Job/pre-post-sync-before`. It stayed there for
  for the 30 s it was watched, past the Job's completion (`Complete 1/1`). Patching
  `phase: Terminating` onto that orphaned state then made no progress for 75 s more.
  So `operation: null` does not end the operation and leaves it orphaned. Interpretation: the controller stops progressing the operation once
  `.operation` is gone (consistent with the next observation, where re-setting
  `.operation` let it finish).
- Observed, setting `.operation` again with the orphaned state present: phase moved
  `Terminating` to `Failed`, message `Operation terminated`, within about 3 s.
- Observed, correct termination on a fresh run: start the sync (`.operation` set), then
  `patch app toy-hooks --type merge -p '{"status":{"operationState":{"phase":"Terminating"}}}'`
  while `.operation` is still present: phase became `Failed`, message
  `Operation terminated`, `.operation` cleared, within about 3 s.
- Conclusion: `operation: null` does not end a running operation and can leave the app
  stuck at `Running`. If ending one is ever needed, patch
  `status.operationState.phase` to `Terminating` while `.operation` is still set; the
  resulting phase is `Failed` with `Operation terminated`. The spec's "wait, never
  terminate" rule is unaffected. Waiting means polling `status.operationState.phase`
  until it is not `Running`.

## 4. Apply semantics (2026-10-08)

- Command: `apply --server-side --field-manager argo-hold-rec --force-conflicts -f ssa.yaml`
  where `ssa.yaml` is a partial Deployment (`name`, `namespace`, `spec.minReadySeconds: 7`),
  on an object created by Argo's client-side apply.
- Observed, first apply: succeeded (`serverside-applied`, `minReadySeconds=7`) but kubectl
  printed `Warning: failed to re-apply configuration after performing Server-Side Apply
  migration ... spec.selector: Required value ...` (non-fatal). `managedFields` afterwards
  listed `argo-hold-rec` (Apply), `kubectl-patch` (Update), `k3s` (Update); the
  `argocd-controller` entry was gone.
- Observed, second identical apply: **failed**, exit 1, `The Deployment "guestbook-ui" is invalid`
  (`spec.selector: Required value`, `spec.template.spec.containers: Required value`,
  `spec.selector: Invalid value: null: field is immutable`).
- Observed, applying the full object (recorded `deployment.json` minus `status`,
  `managedFields`, server-set metadata, and the two annotations) with `minReadySeconds: 9`:
  succeeded, exit 0, no warning.
- Observed, JSON patch `[{"op":"remove","path":"/spec/minReadySeconds"}]`: field gone;
  `argo-hold-rec` stays in `managedFields` (operation `Apply`). App stayed `Synced`.
- Conclusion: a partial manifest applied server-side to an Argo-managed (client-side
  applied) object worked once and failed on the identical second apply. Interpretation:
  the first apply's ownership migration moved the required fields under the apply
  manager, and the second apply then dropped them; this mechanism was not isolated.
  `-f` manifests should be complete objects (for example the live object with the
  change merged in) or the tool should use a patch. The JSON-patch `remove` revert
  worked. After any server-side apply the object keeps an entry for the apply field
  manager (`argo-hold-rec` here; `argo-hold` in the real tool); the revert path is a
  patch, not a re-apply.

## 5. Freeze (2026-10-09)

Argo CD 2.12.4 on a local k3d cluster, app `toy` (guestbook), `selfHeal` on, `--freeze`.

- **A frozen app holds a pending change back.** With the freeze held, `kubectl scale` moved
  `replicas` from 1 to 2. The app went `OutOfSync` and stayed so for 15 s with replicas at 2.
  Without the freeze, Argo reverted the same kind of drift in about a second (section 2).
- **Release syncs it.** `argo-hold release` printed the `NOTICE:` line and restored
  `automated`. Replicas were back at 1 by the time the test polled (0.0 s after `release`
  returned).
- **The hard refresh is not what triggers the sync.** One manual run, outside the suite:
  auto-sync off, replicas scaled to 2, app `OutOfSync`, then `automated` patched back with
  no `argocd.argoproj.io/refresh` annotation. Replicas were back at 1 after 1.1 s and the app
  was `Synced`. One run. The pending change was a live drift reverted by `selfHeal`, not a new
  git commit.
- **Re-enable.** With a freeze held, a manual patch re-enabling `automated` was turned off again
  within the 15 s poll (`test_real_freeze_redisables_auto_sync_when_something_reenables_it`). No
  drift was present, so the test does not show whether a sync can slip in first. Section 2's
  race applies: Argo synced about a second after a re-enable there.

## Not verified

- A freeze of a git-backed app-of-apps: whether pending changes to child Applications all
  sync after release, and whether the hard refresh on the parent propagates to its children.
  NOT RUN: the local harness has no git-backed app-of-apps.
- `ignoreDifferences` plus `RespectIgnoreDifferences` with a manual `automated` change
  (needs a git-backed app-of-apps). NOT RUN: out of scope for the local harness.

## Integration test results (2026-10-08)

Command: `make test-k3d` (setup, `ARGO_HOLD_K3D=1 python3 -m pytest tests -m k3d`,
teardown), context `k3d-argo-hold-test`, real `kubectl`, real Argo CD. Last full run
(2026-10-09, after `--freeze` was added, run as `pytest -m k3d` against a cluster built with
`tests/k3d/setup.sh`): 6 passed in 88 s.

- `test_real_dry_run_computes_paths_and_changes_nothing`: PASSED. Real output lists
  `add /spec/template/spec/tolerations (array(1))` and the Deployment's `resourceVersion`
  is unchanged afterwards. Proves: the dry run computes the path and writes nothing.
- `test_real_hold_redisables_auto_sync_and_release_restores_hand_added_field`: PASSED.
  Proves: after a manual re-enable of auto-sync the holder turns it off again (polled, up to
  15 s), and `release` restores the original `automated` policy and `tolerations`. Does NOT
  prove drift handling: the toleration is a hand-added field that Argo's selfHeal never
  reverts (section 2), so its presence passes with or without the holder. It also does not
  show which path undid the re-enable. With `--interval 5` the watch event and the 5 s
  resync timer can both act inside the window, so an earlier version's claim that this
  exercised the watch path was unsupported and is withdrawn.
- `test_real_hold_reapplies_a_git_managed_field_that_argo_reverts`: PASSED. Holds
  `spec.replicas` of `guestbook-ui` at 2 (git says 1) with `--interval 60`, so the safety
  resync cannot act in the test window and only the watch path can. After auto-sync was
  re-enabled by hand, real Argo reverted replicas and the holder restored both
  `automated`-absent and replicas == 2. The poll saw the two conditions true 0.7 s after
  the re-enable in every run that printed it. That figure is the first poll that saw both true, not a
  measured re-apply latency. Because the poll starts immediately, it can pass before Argo
  has reverted replicas at all, so the poll alone does not prove a revert happened; the
  `argo-hold status` counters show it did, below. `release` restored replicas == 1 and the
  original `automated` policy. Proves: against real Argo, the holder re-disables auto-sync
  and re-applies a git-managed field on watch events with no resync help. Does NOT prove:
  how long Argo takes to revert, or behavior when a sync operation is already in flight
  before the holder re-disables auto-sync.
  - Stability: after fixing a test-side deadline (the first attempt polled status for 20 s,
    but status lags up to one heartbeat interval, see below), the test passed in 5 of 5
    runs: 4 single-test runs against one cluster (one of them `-v`, three consecutive) and
    the `make test-k3d` run; it also passed inside the 4-test full-suite runs.
    The 0.7 s figure came from the 4 single-test runs. No flakiness observed.
  - Status counters observed: `activity: 5 re-disable(s), 4 re-apply(s)` in run 1, then
    `4 re-disable(s), 4 re-apply(s)` in runs 2 and 3, from a single manual re-enable. The
    counters exceed one cycle. Likely causes, not isolated: stale queued watch events that
    still show `automated`, and Argo's in-flight sync reverting replicas again after the
    first re-apply. The final state was correct each time. NOT RUN: a trace that tells
    these two causes apart.
  - `argo-hold status` counters are written to the state file only on the heartbeat tick
    (every `--interval`), so with `--interval 60` status can lag the watch reaction by up
    to 60 s. The test polls up to 75 s for it. A tool-side change (persist on increment) is
    not made here; it is a design question for the owner.
- `test_real_server_side_apply_creates_and_release_deletes`: PASSED. The test asserts
  `argo-hold-it` is absent first, a new ConfigMap created through `-f` appears with
  `data.a == "1"`, and `release` (returncode 0) deletes it.

Strategic-merge notes: the real `kubectl patch --type strategic` on the Deployment worked
for the one shape exercised (a `tolerations` list added where none existed) and Argo's
selfHeal did not remove it, consistent with section 2. Merge-key list behavior (for example
`containers` or `env` merged by name) was NOT RUN, so the fake's handling of those
remains unverified against real kubectl. No tool process was left running after the tests,
and the cluster was deleted (`k3d cluster list` empty).

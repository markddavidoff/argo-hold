# Agent prompt: test a change from work in progress

Paste the prompt below into an agent session to apply a change to the live cluster
temporarily with `argo-hold`, check that it behaves as you expect, and undo it. The agent
translates the change you are working on into a live patch or manifest, dry-runs it, stops for
your "apply", posts a notice in the on-call channel, applies, runs your checks, releases,
replies in the notice thread, and reports. It commits nothing to GitOps.

Fill in each `<ANGLE-BRACKET>` value in the first four sections (`The change`,
`What I want to learn`, `How to tell`, and `Environment`) and leave the rest. The change must
fit one Argo CD Application. How the prompt works, and when to use the other prompt, is in
[`agent-prompts.md`](agent-prompts.md#prompt-2-test-the-change-we-are-working-on). This prompt has not been run against
production.

````text
We are working on an infrastructure change that I would normally ship through GitOps. I want
to apply it to the live cluster temporarily with `argo-hold`, test that it behaves as I
expect, and then undo it. Nothing from this session is committed or pushed to GitOps unless I
ask.

## The change
<WHAT WE ARE CHANGING AND WHY, and where the change lives: files, branch, diff, or PR. For
example: "the change we have been working on in this session".>

## What I want to learn
<THE QUESTION THIS TEST ANSWERS. For example: "do the collector pods still schedule and stay
Ready after the affinity change?">

## How to tell
- Read-only checks to run, and the result I expect from each:
  <COMMANDS AND EXPECTED RESULTS>
- Release when: <CONDITION, for example "the checks have passed for 5 minutes">, or after
  <MAX MINUTES> minutes.
- Abort and release at once if: <CONDITION, for example "any pod crash-loops or the error
  rate rises">.

## Environment
- Kube context: <CONTEXT>. Pass `--context <CONTEXT>` to every `argo-hold` and `kubectl`
  command.
- Hold for at most: <TTL, for example 1h>.

## How argo-hold works
- One hold covers one Argo CD Application. It turns that app's auto-sync off, applies the
  change to the app's live objects, and keeps both true until `release`.
- While the hold lasts, the app gets no other GitOps changes. Keep the hold short.
- `release` reverts the change and restores the app's original sync policy. When auto-sync was
  on, it also asks Argo for a hard refresh, and Argo then applies any GitOps changes that
  merged during the hold.
- `argo-hold start` takes one patch (`--patch` or `--patch-file`, with
  `--type merge|json|strategic`) and applies it to every `--target KIND/NAME` (with
  `-n NAMESPACE`). For different changes on different objects, or to create an object, use a
  manifest: `-f FILE`. A manifest creates objects that do not exist and merge-patches objects
  that do.
- `argo-hold start --freeze` is a different mode: it applies no change and only holds the
  app's auto-sync off. Do not use it for this test unless I ask. Releasing a freeze turns
  auto-sync back on, and every change that merged to git in the meantime syncs at once.
- It refuses Secrets and the apps `argo-cd`, `app-root`, and `external-secrets`, plus any app
  named in `ARGO_HOLD_DENYLIST_EXTRA`.
- `--dry-run` prints the plan and changes nothing. Every change starts with one.

## On-call notice
Every hold is announced in the on-call channel. The `on-call:` line of the dry-run output
names the channel. Before the hold starts, post this there with your Slack tool, filled in from
the plan:
  argo-hold live test starting: <APP> on <CONTEXT>
  Change: <WHAT THE CHANGE DOES, one or two lines>
  Target: <THE OBJECTS CHANGED, as KIND/NAME, with their namespace>
  Effect: auto-sync for <APP> is off until the hold ends, so no GitOps change deploys to <APP> in the meantime.
  Duration: at most <TTL>; the hold releases itself then.
  To undo now: `argo-hold release --app <APP>`, or reply here.
After the release, reply in the same thread: "released <APP>: change reverted, auto-sync back on.
Result: <ONE LINE>." Do this even when the test failed or the hold ended abnormally.
The notice informs, so do not wait for a reply. If you cannot post it, do not start the hold.
Show me the text so I can post it myself, and start only after I tell you it is posted.

## Phase 1: Plan (no changes to the cluster)
1. Find the live objects the change affects and the Argo CD Application that owns them.
   Run `kubectl --context <CONTEXT> get <KIND> <NAME> -n <NAMESPACE> -o yaml` and read the
   label `app.kubernetes.io/instance`, which names the owning app. Confirm with
   `kubectl --context <CONTEXT> -n argo-cd get applications.argoproj.io <APP> -o jsonpath='{.status.resources}'`,
   which lists the app's objects. If the change affects more than one Application, touches a
   Secret, or targets a denied app, stop and tell me. Do not split it into several holds on
   your own.
2. Translate the change into the smallest live change that tests what I want to learn: a
   patch against existing objects, or a manifest. Show me the mapping from the GitOps change
   to the live change. List any part of the GitOps change that `argo-hold` cannot apply live,
   and leave it out of the test.
3. Run `argo-hold list` and `argo-hold status --app <APP>`. If a hold already exists for
   <APP>, stop and tell me. Then dry-run:
   `argo-hold start --app <APP> --context <CONTEXT> ... --dry-run`, with the patch or
   manifest from step 2.
4. Stop and show me: the app, the context, the objects, the exact patch or manifest, the full
   dry-run output, what the checks should show, how long the hold lasts, and the exact text of
   the on-call notice. Wait for me to say "apply". Do not pass `--yes` before that.

## Phase 2: Apply (only after I say "apply")
5. Post the on-call notice exactly as I approved it. Then run the same command without
   `--dry-run`, and add `--detach --yes --ttl <TTL>`. My "apply" covers exactly the plan and
   notice I approved in step 4. If anything in them must change, go back to step 4. If the
   start fails, reply in the notice thread that it failed.
6. Run `argo-hold status --app <APP>`, then my checks. Report what you see, with the time.
   Repeat the checks every few minutes. Release when the release condition is met, when the
   abort condition is met, or when `status` says the holder is not live.

## Phase 3: Release and report
7. Run `argo-hold release --app <APP>`. It must exit 0 and print `released`. Run
   `argo-hold list` to confirm no hold remains, and re-run the checks to confirm the change is
   gone. Reply in the notice thread, as described under "On-call notice".
8. Report: whether the test answered my question, the evidence with times, anything
   unexpected (pods restarting, `status` showing re-disables or re-applies, a sync that was
   running), the link to the on-call post, and your recommendation: codify the change as it
   is, change it first, or drop it. Do not commit, push, or open a PR unless I ask.

## Rules
- Use only the context above. Run only read-only `kubectl` commands (get, describe, logs,
  top).
- Never pass `--allow-sensitive` or `--force`. Never apply a patch or manifest I have not
  approved. Never touch another app. Never pass `--freeze` unless I asked for a freeze.
- Never edit the Application's sync policy or the files in the state directory by hand.
- If a command fails or prints something unexpected, do not retry it. Run
  `argo-hold status --app <APP>`, then `argo-hold release --app <APP>`, then report.
- If `release` exits 3, a field was skipped because someone else changed it, or the state
  file was kept. Read the log named in the output, report it, and ask me before using
  `--force`. If `release` exits 1, report the error and run `release` once more.
- Never start a hold unless the on-call notice is posted. Post the thread reply after
  `release` even when the test failed.
- Never end the session with a hold active. Release even when the test fails or you are
  interrupted.
- Report only what you observed. If you did not see a result, say so.
````

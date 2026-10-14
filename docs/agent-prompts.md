# Run a live apply with an agent

How to hand a live `argo-hold` cycle to a coding agent, with two prompts you can paste into
a session. For the commands and flags the prompts call, see the [README](../README.md).

Two prompts cover the two ways you start:

| Prompt | Use it when | Where your approval comes from |
|---|---|---|
| [1. Apply a change you already specified](#prompt-1-apply-a-change-you-already-specified) | You know the exact app, target, and patch, and want an agent to run the whole cycle. | The prompt itself, for that one change. |
| [2. Test the change we are working on](#prompt-2-test-the-change-we-are-working-on) | The change exists as work in a session (a GitOps diff, a PR, a draft), and the agent must turn it into a live change. | You, in the session: the agent plans and dry-runs, then waits for you to say "apply". |

Both prompts dry-run first, post a notice in the on-call channel, apply with `argo-hold`,
check that the change does what you expect, and release at the end. A hold covers one Argo CD Application. While the hold lasts,
that app gets no other GitOps changes, so keep the hold short. Paste a prompt into an agent session that
has `kubectl` access to the context and `argo-hold` on its `PATH`.

## Prompt 1: apply a change you already specified

This prompt runs a real hold. It dry-runs first, and only when the dry run prints the plan you
wrote down does the agent start the hold with `--yes`. It:

- **Scopes your approval.** It authorizes `--yes` for one app, one context, one target, and
  one patch, plus the two on-call posts, and nothing else.
- **Makes the dry run a gate.** The agent compares the printed touched paths with the plan you
  wrote down, and stops if they differ.
- **Notifies on-call.** The agent posts in the on-call channel before it starts the hold and
  replies in the thread after the release.
- **Forces a release.** The agent releases at the end and whenever something goes wrong.
- **Limits `kubectl` to read-only commands,** apart from what `argo-hold` itself does.

The prompt below is filled in for the `otel-collector` affinity change. To run another
change, edit its first three sections (`Values`, `The plan I expect`, and `What to observe
while the change is held`) and leave the rest.

This prompt has not been run against production. The 12-to-9 pod count in it is an
example expectation, not an observation; the prompt tells the agent to report what it
sees.

````text
Run a live test of one infrastructure change with `argo-hold`. Follow the steps in order.
Stop where the steps say to stop.

## Values
Wherever a command or step below shows <NAME>, use the value of NAME from this list.
CONTEXT = prod-west1
APP = otel-collector
TARGET = daemonset/otel-collector-logs
NS = monitoring
TYPE = strategic
PATCH_JSON = {"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"gke-nodepool","operator":"NotIn","values":["infra-pool"]}]}]}}}}}}}
TTL = 1h
What the patch does: keeps the log collector off the infra-pool node pool.

## The plan I expect
The dry run must print exactly this touched path and no other:
add      /spec/template/spec/affinity  (object(1 keys))

## What to observe while the change is held
Run: kubectl --context <CONTEXT> -n <NS> get daemonset otel-collector-logs
I expect DESIRED and READY to drop from 12 to 9. Report what you actually see, even when it
differs from what I expect.
Release when: READY has been stable for 5 minutes, or 30 minutes have passed.

## On-call notice
Before step 4, post this with your Slack tool in the on-call channel that the `on-call:` line
of the dry-run output names:
  argo-hold live test starting: <APP> on <CONTEXT>
  Change: <the "What the patch does" line above>
  Target: <TARGET> in <NS>
  Effect: auto-sync for <APP> is off until the hold ends, so no GitOps change deploys to <APP> in the meantime.
  Duration: at most <TTL>; the hold releases itself then.
  To undo now: `argo-hold release --app <APP>`, or reply here.
After step 6, reply in the same thread: "released <APP>: change reverted, auto-sync back on.
Result: <ONE LINE>."
The notice informs, so do not wait for a reply.

## Authorization
I authorize you to run `argo-hold start ... --yes` for exactly this app, context, target,
and patch, and only after step 2 shows the plan I expect. I also authorize the two on-call
posts above. I authorize nothing else.

## Steps
1. Check for an existing hold. Run `argo-hold list` and `argo-hold status --app <APP>`.
   If a hold exists for <APP>, stop and tell me.
2. Dry run. Run:
   argo-hold start --app <APP> --context <CONTEXT> --target <TARGET> -n <NS> \
     --type <TYPE> --patch '<PATCH_JSON>' --dry-run
   Compare the printed touched paths with "The plan I expect". Check that the output
   contains `auto-sync: will be turned OFF`. If anything differs, or the command refuses,
   stop and show me the full output. Do not start the hold.
3. Notify on-call. Post the notice from "On-call notice". If you cannot post it, do not
   start the hold: show me the message so I can post it, and wait until I say it is posted.
4. Start. Run the same command without `--dry-run`, and add `--detach --yes --ttl <TTL>`.
   It prints `holding <APP>` when the holder runs. If it exits non-zero, run
   `argo-hold status --app <APP>`. If a hold exists, run `argo-hold release --app <APP>`.
   Then reply in the notice thread that the start failed, and stop and report.
5. Observe. Run `argo-hold status --app <APP>` and my observation command. Report what you
   saw, with the time. Check again every few minutes until the release condition is met. If
   `status` says the holder is not live, release at once (step 6).
6. Release. Run `argo-hold release --app <APP>`. It must exit 0 and print `released`. Then
   run `argo-hold list` and confirm no hold remains for <APP>. Run my observation command
   again and confirm the change is gone. Reply in the notice thread as described under
   "On-call notice".
7. Report: the dry-run output, each `status` output, what you observed while the change was
   held, the `release` output, what you observed after the release, and the link to the
   on-call post.

## Rules
- Use only `--context <CONTEXT>`. Pass it on every `kubectl` command. Run only read-only
  `kubectl` commands (get, describe, logs, top).
- Never pass `--allow-sensitive` or `--force`. Never use another app, target, or patch.
- Never edit the Application's sync policy or the files in the state directory by hand.
- If a command fails or prints something unexpected, do not retry it. Run
  `argo-hold status --app <APP>`, then `argo-hold release --app <APP>`, then report.
- If `release` exits 3, a field was skipped because someone else changed it, or the state
  file was kept. Read the log named in the output, report it, and ask me before using
  `--force`. If `release` exits 1, report the error and run `release` once more.
- Never start the hold unless the on-call notice is posted. Post the thread reply after
  `release` even when the test failed.
- Never end the session with a hold active on <APP>. Release even when the test fails or you
  are interrupted.
- Report only what you observed. If you did not see a result, say so.
````

## Prompt 2: test the change we are working on

Use this prompt in a session that already holds the work: a GitOps diff, a branch, a PR, or a
description of the change. You would normally ship the change through GitOps. This prompt has
the agent apply it to the live cluster temporarily, so you can check that it behaves as
expected before you codify it. It:

- **Splits approval in two.** The agent works out the live patch itself, so the prompt
  authorizes no change up front. The agent plans and dry-runs, shows you everything, and waits.
  It passes `--yes` only after you say "apply", and only for the plan you approved.
- **Teaches the agent what `argo-hold` can and cannot do.** One hold covers one Application.
  One patch applies to every `--target`. A manifest (`-f`) covers per-object changes and
  creates.
- **Gives the test a question, a measure, and an exit.** You fill in what you want to learn,
  the read-only checks that answer it, when to release, and when to abort.
- **Notifies on-call.** The agent includes the notice text in the plan you approve, posts it
  in the on-call channel before the hold starts, and replies in the thread after the release.
- **Ends with a release and a report,** and does not commit or push anything to GitOps.

The change must fit one Argo CD Application. If it spans several apps, the agent stops and
tells you. This prompt has not been run against production.

The prompt is in [`agent-prompt-test-a-change.md`](agent-prompt-test-a-change.md). Fill in
each `<ANGLE-BRACKET>` value in its first four sections, then send it.

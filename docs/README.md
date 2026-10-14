# argo-hold documentation

Start with the [project README](../README.md) for what `argo-hold` is, a quick start, and the
command reference. These pages go deeper.

| Page | What it covers |
|---|---|
| [how-it-works.md](how-it-works.md) | The start sequence, the holder (watchers, reactor, timer), the no-clobber revert, freeze mode, failure modes, and limitations. |
| [rbac.md](rbac.md) | The minimal Kubernetes Role derived from the tool's `kubectl` calls, with `kubectl auth can-i` checks. |
| [k3d-findings.md](k3d-findings.md) | What the throwaway k3d + Argo CD runs observed, including the evidence behind the design rules. |
| [agent-prompts.md](agent-prompts.md) | Two prompts that hand a full hold cycle to a coding agent. |
| [agent-prompt-test-a-change.md](agent-prompt-test-a-change.md) | The fill-in prompt referenced by prompt 2. |

Release and change history live in [CHANGELOG.md](../CHANGELOG.md) and
[RELEASING.md](../RELEASING.md).

# Changelog

## 0.1.0

First public release.

- `start`, `status`, `list`, `release`: hold one Argo CD Application's auto-sync off while a
  live patch or manifest stays applied, then revert it from the state file.
- `--freeze`: turn auto-sync off with no change applied.
- Refuses `Secret` targets and the `argo-cd`, `app-root`, and `external-secrets` apps
  (override with `--allow-sensitive`); `ARGO_HOLD_DENYLIST_EXTRA` adds more apps.
- `ARGO_HOLD_ONCALL_CHANNEL` names the channel the plan output tells you to notify.

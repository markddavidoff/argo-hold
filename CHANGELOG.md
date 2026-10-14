# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0

First public release.

- `start`, `status`, `list`, `release`: hold one Argo CD Application's auto-sync off while a
  live patch or manifest stays applied, then revert it from the state file.
- `--freeze`: turn auto-sync off with no change applied.
- `--version` / `-V`: print `argo-hold 0.1.0`.
- `--argocd-namespace` and `ARGO_HOLD_ARGOCD_NAMESPACE`: set the namespace Argo CD runs in
  for `start` (default `argo-cd`; a stock Argo CD install uses `argocd`). `status` and
  `release` use the namespace recorded in the state file.
- Refuses `Secret` targets and the `argo-cd`, `app-root`, and `external-secrets` apps
  (override with `--allow-sensitive`); `ARGO_HOLD_DENYLIST_EXTRA` adds more apps.
- `ARGO_HOLD_ONCALL_CHANNEL` names the channel the plan output tells you to notify.

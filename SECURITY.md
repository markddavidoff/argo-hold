# Security policy

`argo-hold` runs `kubectl` against live clusters and temporarily changes an Argo CD
Application's sync policy. A flaw that lets it touch objects it should refuse (a `Secret`, a
denylisted app), leaves auto-sync off after `release`, or leaks held values is a security
issue.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/markddavidoff/argo-hold/security/advisories/new).
Do not open a public issue for a vulnerability. Expect an acknowledgement within a week.

## Scope notes

- The state file under `~/.local/state/argo-hold` stores the values a hold changes, which is
  why the tool refuses `Secret` targets. Treat that directory as sensitive anyway.
- The tool's only network access is through your `kubectl` context.

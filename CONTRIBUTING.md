# Contributing

Issues and pull requests are welcome.

## Setup

Python 3.10 or newer. The tool uses only the standard library; tests need `pytest`.

```bash
python3 -m pip install pytest
make test        # fake kubectl, no cluster needed
make test-k3d    # optional: real k3d + Argo CD (docker, k3d, network)
uvx ruff check .
```

## Ground rules

- Keep the tool standard-library only.
- A change to what `start` or `release` writes to the cluster needs a test against the fake
  cluster and an update to the "What it changes in the cluster" table in `README.md`.
- Safety checks (the denylist, the `Secret` refusal, the one-hold-per-app lock) get a test
  for the refusal and for the override.
- Update `docs/how-it-works.md` when behavior or failure modes change.
- Commit messages: a short imperative summary, e.g. `fix: restore policy when the holder is killed`.

# Releasing argo-hold

These are the steps the maintainer runs to cut a release. Nothing here runs in CI, and no
reviewer or agent should tag or publish a release; that is a manual, maintainer-only step.

The version lives in two files, and `tests/test_version.py` fails if they disagree:

- `pyproject.toml` -> `version`
- `scripts/ah_version.py` -> `__version__`

## 1. Prepare the release commit

1. Bump the version in both files above to the new `X.Y.Z`.
2. Move the `CHANGELOG.md` entry for the release under its version heading, and date it.
3. Run the gates:
   ```bash
   python3 -m pytest tests
   uvx ruff check .
   ```
4. Commit on a branch, open a PR, and merge it once CI is green.

## 2. Tag and publish

Run these on `main` at the merged release commit:

```bash
git tag -a v0.1.0 -m "argo-hold 0.1.0"
git push origin v0.1.0
gh release create v0.1.0 --title "argo-hold 0.1.0" --notes-from-tag
```

GitHub builds the source tarball for the tag at:

```
https://github.com/markddavidoff/argo-hold/archive/refs/tags/v0.1.0.tar.gz
```

## 3. Update the Homebrew formula

The formula lives in the tap repo `markddavidoff/homebrew-tap` as `Formula/argo-hold.rb`.
`packaging/homebrew/argo-hold.rb` in this repo is the template to copy there. It installs
from the release tarball, so its `url` and `sha256` change on every release.

1. Compute the tarball checksum:
   ```bash
   curl -fL https://github.com/markddavidoff/argo-hold/archive/refs/tags/v0.1.0.tar.gz \
     | shasum -a 256
   ```
2. In `Formula/argo-hold.rb` in the tap repo, set:
   - `url` to the `v0.1.0.tar.gz` URL above.
   - `sha256` to the checksum from step 1.
3. Check the formula, then commit and push the tap:
   ```bash
   ruby -c Formula/argo-hold.rb
   brew audit --strict --online markddavidoff/tap/argo-hold   # needs brew installed
   brew install markddavidoff/tap/argo-hold
   argo-hold --version                                        # prints: argo-hold 0.1.0
   ```

## 4. Announce

Confirm the release badge on the README shows the new version and that
`brew install markddavidoff/tap/argo-hold` installs it and `argo-hold --version` prints it.

## Manual, one-time repo settings

These cannot be set through the GitHub API and must be done once in the web UI:

- **Social preview image:** GitHub **Settings -> General -> Social preview**. Upload a PNG
  (recommended 1280x640). Not faked or scripted here.
- **Regenerate the demo GIF** after any change to the CLI output it shows: run `make demo`
  (needs `vhs`) and commit the new `demo/demo.gif`.

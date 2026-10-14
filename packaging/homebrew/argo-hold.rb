# Homebrew formula template for argo-hold.
#
# This is NOT installed from this repo. Copy it to the tap repo
# `markddavidoff/homebrew-tap` as `Formula/argo-hold.rb`, then fill in `url` and `sha256`
# for the published release (see RELEASING.md). Until then the two placeholders below are
# intentionally invalid so an accidental `brew install` fails loudly rather than silently
# pulling the wrong tarball.
#
# argo-hold is a stdlib-only Python CLI: the formula installs the `scripts/` directory into
# libexec and symlinks the `argo-hold` launcher onto PATH. The launcher resolves symlinks
# and runs `argo_hold.py` from next to itself, so the whole directory must travel together.
class ArgoHold < Formula
  desc "Hold one Argo CD app's auto-sync off while a live change stays applied"
  homepage "https://github.com/markddavidoff/argo-hold"
  # TODO: after `gh release create v0.1.0`, set url + sha256 (RELEASING.md step 3).
  url "https://github.com/markddavidoff/argo-hold/archive/refs/tags/REPLACE_WITH_TAG.tar.gz"
  sha256 "REPLACE_WITH_SHA256_OF_THE_RELEASE_TARBALL"
  license "MIT"

  depends_on "python@3.12"

  def install
    libexec.install Dir["scripts/*"]
    # The launcher runs `python3` from PATH, so put the formula's Python first.
    (bin/"argo-hold").write_env_script libexec/"argo-hold",
      PATH: "#{Formula["python@3.12"].opt_bin}:$PATH"
  end

  test do
    assert_match "argo-hold #{version}", shell_output("#{bin}/argo-hold --version")
  end
end

"""The version string has one source of truth. ah_version carries it at runtime and
pyproject.toml carries it for packaging; this test fails if they drift apart."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ah_version  # noqa: E402

LAUNCHER = ROOT / "scripts" / "argo-hold"


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "no version found in pyproject.toml"
    return m.group(1)


def test_code_and_pyproject_versions_agree():
    assert ah_version.__version__ == _pyproject_version()


def test_version_flag_prints_the_version():
    r = subprocess.run([str(LAUNCHER), "--version"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"argo-hold {ah_version.__version__}"


def test_short_version_flag_prints_the_version():
    r = subprocess.run([str(LAUNCHER), "-V"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"argo-hold {ah_version.__version__}"

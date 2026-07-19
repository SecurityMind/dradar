import tomllib
from pathlib import Path

from dradar import __version__


def test_runtime_version_matches_package_metadata():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text())
    assert __version__ == metadata["project"]["version"]

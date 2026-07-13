"""Tests always import the SOURCE tree, never a site-packages copy.

Same invariant as dradar-server's conftest, for the same reason (learned the
hard way multiple times): a stale regular install in whatever venv runs
pytest makes the whole suite silently test old code with zero errors — and
on this machine editable installs are unreliable (the sandbox keeps stamping
macOS UF_HIDDEN on the generated .pth, which CPython's site.py then skips).
Pinning src/ at the front of sys.path removes the failure mode regardless of
how (or whether) the package is installed in the interpreter.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

"""Task content hash: reproducibility/tamper-evidence for what the agent sees.

Hashes only instruction.md, task.toml, and environment/ — never solution/ or
tests/, since a volunteer's local deep-swe checkout is a full public clone
that contains those too. Shared verbatim by server and client so both sides
compute the exact same digest.
"""

import hashlib
from pathlib import Path


def task_content_hash(deep_swe_tasks_dir: Path, task_id: str) -> str:
    task_dir = Path(deep_swe_tasks_dir) / task_id
    digest = hashlib.sha256()
    for name in ("instruction.md", "task.toml"):
        path = task_dir / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        rel_paths = sorted(p.relative_to(env_dir).as_posix() for p in env_dir.rglob("*") if p.is_file())
        for rel in rel_paths:
            digest.update(rel.encode())
            digest.update((env_dir / rel).read_bytes())
    return digest.hexdigest()

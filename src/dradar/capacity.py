"""Conservative local worker recommendation.

The Docker engine's limits are more relevant than the host's headline specs:
Docker Desktop/OrbStack may expose only part of the host CPU and memory.  The
recommendation is deliberately a floor, not a benchmark claim.  A DeepSWE
build can spike far above its steady-state usage, so normal users never get an
automatic recommendation above four workers even on a very large machine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


CPU_PER_WORKER = 2
MEM_GIB_PER_WORKER = 6
DOCKER_MEM_RESERVE_GIB = 2
FIRST_WORKER_DISK_GIB = 20
EXTRA_WORKER_DISK_GIB = 12
AUTO_WORKER_CAP = 4


@dataclass(frozen=True)
class CapacityReport:
    recommended_workers: int
    docker_cpus: int | None
    docker_memory_gib: float | None
    disk_free_gib: float
    account_limit: int
    held_tasks: int
    task_limit: int
    cpu_limit: int
    memory_limit: int
    disk_limit: int
    warnings: tuple[str, ...] = ()


def _docker_resources() -> tuple[int | None, float | None, tuple[str, ...]]:
    docker = shutil.which("docker")
    if not docker:
        return None, None, ("docker CLI not found; falling back to 1 worker",)
    try:
        proc = subprocess.run(
            [docker, "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None, ("cannot inspect Docker resources; falling back to 1 worker",)
    if proc.returncode != 0:
        return None, None, ("Docker daemon is unavailable; falling back to 1 worker",)
    try:
        info = json.loads(proc.stdout)
        cpus = int(info.get("NCPU") or 0)
        memory = int(info.get("MemTotal") or 0) / (1024 ** 3)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, ("Docker returned unreadable capacity data; falling back to 1 worker",)
    if cpus < 1 or memory <= 0:
        return None, None, ("Docker omitted CPU/memory capacity; falling back to 1 worker",)
    return cpus, memory, ()


def inspect_capacity(client, requested_tasks: int | None = None) -> CapacityReport:
    """Inspect Docker + disk + server limits without claiming any task."""
    me = client.whoami()
    assignment_data = client.get_assignment()
    active = assignment_data.get("active")
    if active is None:
        active = [assignment_data["assignment"]] if assignment_data.get("assignment") else []
    held = len(active)
    account_limit = max(1, int(
        me.get("concurrent_limit") or me.get("claim_limit") or 1))
    task_limit = max(1, int(requested_tasks or held or account_limit))

    cpus, memory_gib, warnings = _docker_resources()
    disk_gib = shutil.disk_usage(Path.home()).free / (1024 ** 3)
    if cpus is None or memory_gib is None:
        cpu_limit = memory_limit = 1
    else:
        cpu_limit = max(1, cpus // CPU_PER_WORKER)
        memory_limit = max(
            1, int((memory_gib - DOCKER_MEM_RESERVE_GIB) // MEM_GIB_PER_WORKER))
    disk_limit = max(
        1,
        1 + int(max(0.0, disk_gib - FIRST_WORKER_DISK_GIB)
                // EXTRA_WORKER_DISK_GIB),
    )
    recommended = max(1, min(
        cpu_limit, memory_limit, disk_limit, account_limit, task_limit,
        AUTO_WORKER_CAP,
    ))
    return CapacityReport(
        recommended_workers=recommended,
        docker_cpus=cpus,
        docker_memory_gib=memory_gib,
        disk_free_gib=disk_gib,
        account_limit=account_limit,
        held_tasks=held,
        task_limit=task_limit,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        disk_limit=disk_limit,
        warnings=warnings,
    )


def print_report(report: CapacityReport) -> None:
    docker = "unavailable"
    if report.docker_cpus is not None and report.docker_memory_gib is not None:
        docker = (f"{report.docker_cpus} CPU / "
                  f"{report.docker_memory_gib:.1f} GiB memory")
    print("local worker capacity:")
    print(f"  Docker: {docker}")
    print(f"  disk free: {report.disk_free_gib:.0f} GiB")
    print(f"  account concurrency limit: {report.account_limit}")
    print(f"  currently held tasks: {report.held_tasks}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    print(f"recommended workers: {report.recommended_workers} "
          "(conservative; each task can spike during builds)")


def cmd_capacity(args) -> int:
    # Local imports avoid making a read-only machine probe participate in the
    # identity module's import graph.
    from .api_client import ApiError
    from .identity import _client
    from .local_config import _load_config

    try:
        report = inspect_capacity(_client(_load_config()))
    except ApiError as exc:
        raise SystemExit(f"capacity check failed: {exc}") from exc
    print_report(report)
    return 0


__all__ = ["CapacityReport", "cmd_capacity", "inspect_capacity", "print_report"]

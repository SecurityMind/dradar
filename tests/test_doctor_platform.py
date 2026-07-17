"""Platform-aware doctor: right fix hints for macOS / Linux / WSL2 / Windows."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import doctor


def test_platform_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor._platform() == "macos"


def test_platform_windows_native(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert doctor._platform() == "windows"


def test_platform_wsl_detected_via_proc_version(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    proc = tmp_path / "version"
    proc.write_text("Linux version 5.15.153.1-microsoft-standard-WSL2 ...")
    assert doctor._platform(proc_version=proc) == "wsl"


def test_platform_bare_linux(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    proc = tmp_path / "version"
    proc.write_text("Linux version 6.8.0-45-generic (buildd@lcy02) ...")
    assert doctor._platform(proc_version=proc) == "linux"


def test_platform_linux_without_proc_version(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert doctor._platform(proc_version=tmp_path / "missing") == "linux"


def _run_doctor(monkeypatch, capsys, plat: str) -> tuple[int, str]:
    monkeypatch.setattr(doctor, "_platform", lambda: plat)
    # No tools on PATH and no config: every check FAILs, printing every hint.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    rc = doctor.cmd_doctor(SimpleNamespace())
    return rc, capsys.readouterr().out


def test_doctor_linux_hints_are_linux(monkeypatch, capsys):
    rc, out = _run_doctor(monkeypatch, capsys, "linux")
    assert rc == 1
    assert "docs.docker.com/engine/install" in out
    assert "usermod -aG docker" in out
    assert "npm install -g @openai/codex" in out
    assert "OrbStack" not in out and "brew" not in out


def test_doctor_wsl_hints_mention_docker_desktop_integration(monkeypatch, capsys):
    rc, out = _run_doctor(monkeypatch, capsys, "wsl")
    assert rc == 1
    assert "WSL integration" in out
    assert "OrbStack" not in out


def test_doctor_macos_hints_unchanged(monkeypatch, capsys):
    rc, out = _run_doctor(monkeypatch, capsys, "macos")
    assert rc == 1
    assert "brew install --cask orbstack" in out
    assert "brew install codex" in out


def test_doctor_native_windows_runs_real_preflight_with_native_hints(
    monkeypatch, capsys,
):
    rc, out = _run_doctor(monkeypatch, capsys, "windows")
    assert rc == 1
    assert "native Windows support is experimental" in out
    assert "docker CLI" in out
    assert "Docker.DockerDesktop" in out
    assert "chatgpt.com/codex/install.ps1" in out
    assert "wsl --install" not in out
    assert "Ubuntu" not in out

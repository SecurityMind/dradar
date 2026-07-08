import dradar.runner as runner_mod
from dradar.runner import CLAUDE_DISALLOWED_TOOLS, build_pier_command


def _assignment(agent, model="gpt-5.5", effort="medium"):
    return {"assignment_id": "a1", "task_id": "abs-module-cache-flags",
            "agent": agent, "model": model, "effort": effort}


def _stub_pier(monkeypatch):
    # build_pier_command resolves pier via shutil.which; stub it so the test
    # doesn't depend on pier being on the runner's PATH.
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")


def test_codex_disables_web_search(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    # make the local task path exist so build_pier_command doesn't bail
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    build_pier_command(_assignment("codex"), tmp_path, tmp_path / "jobs", "j", home)
    allowlist = (home / "codex-chatgpt-allowlist.toml").read_text()
    # web_search must be a top-level string key BEFORE any [table] header, or
    # TOML nests it and codex ignores it (verified: bool/nested = no effect).
    assert 'web_search = "disabled"' in allowlist
    assert allowlist.index("web_search") < allowlist.index("[__pier_allowlist]")


def test_claude_code_disallows_web_tools(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    cmd = build_pier_command(_assignment("claude-code", model="claude-sonnet-5", effort="high"),
                             tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}" in cmd
    assert "WebSearch" in CLAUDE_DISALLOWED_TOOLS and "WebFetch" in CLAUDE_DISALLOWED_TOOLS

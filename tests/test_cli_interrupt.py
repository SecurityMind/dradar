"""The Ctrl-C/EOF choke point in cli.main(): lease guidance only where it's
true (go/resume/retry-upload); a cancelled login gets a plain 'interrupted'
instead of being sent to `dradar resume` on a possibly-unconfigured machine."""

from dradar import cli


def _interrupt(*_a, **_k):
    raise KeyboardInterrupt


def test_interrupted_go_mentions_leases_and_resume(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_go", _interrupt)
    rc = cli.main(["go", "-y"])
    out = capsys.readouterr().out
    assert rc == 130
    assert "held leases stay active" in out and "dradar resume" in out


def test_interrupted_login_gets_plain_message(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_login", _interrupt)
    rc = cli.main(["login", "--github"])
    out = capsys.readouterr().out
    assert rc == 130
    assert "interrupted" in out
    assert "resume" not in out

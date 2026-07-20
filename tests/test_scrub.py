from dradar.scrub import (
    patch_structure_is_valid, redact_patch_secrets, scan_secrets, scrub_bytes,
    scrub_text,
)


def test_scrubs_openai_key():
    assert "sk-" not in scrub_text("key=sk-proj-abc123def456ghi789jkl000")


def test_scrubs_anthropic_key_with_correct_label():
    out = scrub_text("token: sk-ant-api03-xxxxxxxxxxxxxxxxxxxxx")
    assert "sk-ant-api03" not in out
    assert "[REDACTED-SK-ANT]" in out  # not mislabeled as generic SK


def test_scrubs_email_and_home():
    out = scrub_text("aloha@example.com wrote /Users/aloha/x and /home/bob/y")
    assert "aloha@example.com" not in out
    assert "/Users/aloha" not in out and "/home/bob" not in out
    assert "/[HOME]/x" in out


def test_scrubs_opaque_fernet_token():
    tok = "gAAAAABm" + "Zk9" * 20
    assert tok not in scrub_text(f"session={tok}")


def test_keeps_normal_code():
    code = "def apply(x):\n    return x + 1  # normal comment\n"
    assert scrub_text(code) == code


def test_scrub_bytes_never_bypasses_on_bad_utf8():
    # A secret next to an invalid UTF-8 byte must still be redacted (not
    # written verbatim as the old UnicodeDecodeError fallback did).
    data = b"sk-proj-abc123def456ghi789xyz \x80\xff tail"
    out = scrub_bytes(data)
    assert b"sk-proj" not in out
    assert b"\x80\xff" in out  # non-secret bytes round-trip intact


def test_scan_secrets_detects_without_rewriting():
    assert scan_secrets(b"+ api_key = sk-proj-abc123def456ghi789xyz\n")
    assert scan_secrets(b"ghp_" + b"a" * 30)
    assert scan_secrets(b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NQ.abcdefghijklmnop")


def test_scan_secrets_clean_patch_passes():
    patch = b"diff --git a/x b/x\n@@ -1 +1 @@\n-old value\n+new value\n"
    assert scan_secrets(patch) == []


def test_patch_redaction_only_rewrites_added_hunk_lines():
    patch = b"""diff --git a/app.py b/app.py
index 3367afd..f04ce62 100644
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 old = True
+api_key = \"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\"
"""
    redacted, labels, unsafe = redact_patch_secrets(patch)
    assert "GHP" in labels and unsafe == []
    assert b"ghp_" not in redacted
    assert b'api_key = "[REDACTED-GHP]"' in redacted
    assert scan_secrets(redacted) == []
    assert patch_structure_is_valid(redacted)


def test_patch_redaction_rejects_secret_in_context_line():
    patch = b"""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456
+safe = True
"""
    redacted, labels, unsafe = redact_patch_secrets(patch)
    assert labels == [] and "GHP" in unsafe
    assert b"ghp_" in redacted

"""GitHub device-flow helper (client side).

The volunteer's machine talks to GitHub directly to obtain an access token:
request a device+user code, show the user the code + verification URL, then
poll until they authorize in their browser. No client secret is needed for
device flow, and the default scope is empty (public profile only). The token
is handed to the dradar server for a single identity read and never stored.
"""

import time
import urllib.error
import urllib.parse
import urllib.request

_DEVICE_URL = "https://github.com/login/device/code"
_TOKEN_URL = "https://github.com/login/oauth/access_token"


class DeviceFlowError(RuntimeError):
    pass


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Accept": "application/json", "User-Agent": "dradar"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            import json
            return json.loads(resp.read())
    except (urllib.error.URLError, ValueError) as exc:
        raise DeviceFlowError(f"GitHub request failed: {exc}") from exc


def start(client_id: str) -> dict:
    """Returns {device_code, user_code, verification_uri, interval, expires_in}."""
    out = _post_form(_DEVICE_URL, {"client_id": client_id})
    if "device_code" not in out:
        raise DeviceFlowError(f"unexpected GitHub response: {out}")
    return out


def poll(client_id: str, device_code: str, interval: int,
         expires_in: int, sleep=time.sleep, clock=time.monotonic) -> str:
    """Block until the user authorizes; returns the access token. Honors
    GitHub's slow_down backoff and the code's expiry."""
    deadline = clock() + expires_in
    wait = max(1, interval)
    while clock() < deadline:
        sleep(wait)
        out = _post_form(_TOKEN_URL, {
            "client_id": client_id, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if out.get("access_token"):
            return out["access_token"]
        err = out.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            wait = int(out.get("interval", wait + 5))
            continue
        if err in ("expired_token", "access_denied"):
            raise DeviceFlowError(f"authorization {err}")
        # unknown error: surface it rather than spin
        raise DeviceFlowError(out.get("error_description") or err or "unknown error")
    raise DeviceFlowError("the device code expired before you authorized")

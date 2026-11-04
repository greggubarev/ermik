"""Authorize a cross-owner PR broker through GitHub App Device Flow."""

from __future__ import annotations

import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .app_token_rotation import TokenPair, TokenRotationError, TokenStore


def _post(path: str, fields: dict[str, str], opener=urlopen) -> dict:
    request = Request(
        "https://github.com/login/" + path,
        data=urlencode(fields).encode("ascii"),
        method="POST",
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "ermik-pr-broker"},
    )
    try:
        with opener(request, timeout=20) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        return result
    except (HTTPError, URLError, OSError, ValueError, UnicodeError) as exc:
        raise TokenRotationError("Device Flow request failed; retry authorization") from None


def authorize(client_id: str, store: TokenStore, *, reauthorize: bool = False,
              opener=urlopen, sleep=time.sleep, now=time.time,
              announce=print) -> None:
    if not client_id or any(ch.isspace() for ch in client_id):
        raise TokenRotationError("GitHub App client ID invalid")
    device = _post("device/code", {"client_id": client_id,
                                   "scope": "offline_access"}, opener)
    code = device.get("device_code")
    user_code = device.get("user_code")
    uri = device.get("verification_uri")
    lifetime = device.get("expires_in")
    interval = device.get("interval")
    if (not isinstance(code, str) or not code or not isinstance(user_code, str)
            or not user_code or uri != "https://github.com/login/device"
            or type(lifetime) is not int or lifetime <= 0
            or type(interval) is not int or interval < 1):
        raise TokenRotationError("Device Flow response invalid; retry authorization")
    deadline = now() + lifetime
    announce(f"Open {uri} and enter code {user_code}")
    while now() < deadline:
        sleep(interval)
        result = _post("oauth/access_token", {
            "client_id": client_id, "device_code": code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, opener)
        error = result.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = max(interval + 5, result.get("interval", 0)
                           if type(result.get("interval")) is int else 0)
            continue
        if error:
            raise TokenRotationError("Device Flow authorization declined or expired")
        access, refresh = result.get("access_token"), result.get("refresh_token")
        access_life = result.get("expires_in")
        refresh_life = result.get("refresh_token_expires_in")
        if (not isinstance(access, str) or not access
                or not isinstance(refresh, str) or not refresh
                or type(access_life) is not int or access_life <= 3600
                or type(refresh_life) is not int or refresh_life <= access_life
                or result.get("token_type") != "bearer"):
            raise TokenRotationError(
                "expiring token pair unavailable; check App settings and retry authorization"
            )
        issued = int(now())
        pair = TokenPair(access, refresh, issued + access_life,
                         issued + refresh_life)
        if reauthorize:
            store.reauthorize(pair)
        else:
            store.initialize(pair)
        announce("Broker credential saved in the private token store")
        return
    raise TokenRotationError("Device Flow code expired; retry authorization")


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize a GitHub App PR broker")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--store", type=str, required=True)
    parser.add_argument("--reauthorize", action="store_true")
    args = parser.parse_args()
    try:
        authorize(args.client_id, TokenStore(args.store),
                  reauthorize=args.reauthorize)
    except TokenRotationError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

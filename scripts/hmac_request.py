#!/usr/bin/env python3
"""Send a request carrying the Forge HMAC second factor.

The scheme is deliberately tiny::

    payload   = HTTP_METHOD + HTTP_FULL_PATH      e.g. "POST/api/v2/publish?dry_run=1"
    signature = hex(HMAC_SHA256(api_secret, payload))   ->  X-Forge-Signature

``api_secret`` is the secret paired with the WeKnora API key (X-API-Key), resolved
from ``--api-secret``, then ``$WEKNORA_API_SECRET``, then the ``keys.json`` entry
for the key (``--keys-file``, else next to ``config.json`` via ``$FORGE_CONFIG``,
else ``data/keys.json``).  ``--api-key`` defaults to ``$WEKNORA_API_KEY``; when the
environment is not exported, both values are read from the repository ``.env``.

There is no timestamp, no nonce and no body digest: freshness comes from the
signature being single use for state-changing methods (POST/PUT/PATCH/DELETE) -
simply re-sending the same request is rejected as a replay.

Usage::

    python scripts/hmac_request.py --base http://localhost:8000 --api-key sk-xxxxx \
        --api-secret <secret> POST /api/v2/publish --json '{"kb_id":"kb-1","title":"t","content":"c"}'

    # secret falls back to $WEKNORA_API_SECRET / keys.json when omitted
    python scripts/hmac_request.py --api-key sk-xxxxx \
        GET '/api/v2/knowledge/search?q=level%20%3E%3D%203'

    # print the headers only, to paste into curl / Postman / a job runner
    python scripts/hmac_request.py --api-key sk-xxxxx --dry-run GET /api/v2/whoami

Sign the path EXACTLY as it goes on the wire, percent-encoding included: Forge signs the
raw path and the raw query string and never normalises either, so an HTTPS terminating
proxy in front (Cloudflare) cannot break the signature - the scheme and the host are not
part of the payload.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict


def signature_payload(method: str, path: str) -> str:
    """METHOD + FULL_PATH, concatenated without a separator."""
    return f"{method.upper()}{path}"


def build_signature(method: str, path: str, api_secret: str) -> str:
    payload = signature_payload(method, path).encode("utf-8")
    return hmac.new(api_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _split_header(raw: str) -> tuple:
    """Accept both "Name: value" and "Name=value"."""
    for separator in ("=", ":"):
        name, found, value = raw.partition(separator)
        if found:
            return name.strip(), value.strip()
    return "", ""


def _fill_from_env_file() -> None:
    """Fill WEKNORA_API_KEY / WEKNORA_API_SECRET from the repo .env when absent."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if (
            name in {"WEKNORA_API_KEY", "WEKNORA_API_SECRET"}
            and not os.environ.get(name, "").strip()
        ):
            os.environ[name] = value.strip().strip("'\"")


def _keys_file_candidates(explicit: str = "") -> list:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
        return candidates
    override = os.environ.get("FORGE_CONFIG", "").strip()
    if override:
        path = Path(override)
        candidates.append(
            path / "keys.json" if path.is_dir() else path.parent / "keys.json"
        )
    candidates.append(Path("data/keys.json"))
    candidates.append(Path(__file__).resolve().parent.parent / "data" / "keys.json")
    return candidates


def _load_keys_file(explicit: str = "") -> Dict[str, str]:
    for path in _keys_file_candidates(explicit):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            pairs: Dict[str, str] = {}
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("api_key") or "").strip()
                secret = str(entry.get("api_secret") or "").strip()
                if key and secret:
                    pairs[key] = secret
            return pairs
    return {}


def resolve_secret(api_key: str, api_secret: str = "", keys_file: str = "") -> str:
    """Resolve the HMAC signing secret for ``api_key`` (see module docstring)."""
    if api_secret:
        return api_secret
    env_key = os.environ.get("WEKNORA_API_KEY", "").strip()
    env_secret = os.environ.get("WEKNORA_API_SECRET", "").strip()
    if env_secret and (not env_key or env_key == api_key):
        return env_secret
    return _load_keys_file(keys_file).get(api_key, "")


def build_headers(method: str, path: str, api_key: str, api_secret: str) -> dict:
    headers = {
        "X-Forge-Signature": build_signature(method, path, api_secret),
        "Content-Type": "application/json",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def main() -> int:
    _fill_from_env_file()
    parser = argparse.ArgumentParser(description="Forge HMAC signed request helper")
    parser.add_argument("method", help="GET / POST / PUT / PATCH / DELETE ...")
    parser.add_argument(
        "path", help="Path starting with /, including the query string if any"
    )
    parser.add_argument(
        "--base", default="http://localhost:8000", help="Service base URL"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("WEKNORA_API_KEY", ""),
        help="WeKnora API key sent as X-API-Key (default: $WEKNORA_API_KEY)",
    )
    parser.add_argument(
        "--api-secret",
        default="",
        help="HMAC signing secret; falls back to $WEKNORA_API_SECRET, then keys.json",
    )
    parser.add_argument(
        "--keys-file",
        default="",
        help="Explicit path to keys.json (default: next to config.json, then data/keys.json)",
    )
    parser.add_argument(
        "--json",
        dest="json_body",
        default="",
        help="JSON string, or @file to read the body from a file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the signature headers only"
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="Request timeout in seconds"
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Extra request header, repeatable. Handy to replay what a proxy sends, e.g. "
        "--header 'X-Forwarded-Proto: https' --header 'CF-Connecting-IP: 198.51.100.9'",
    )
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key is required (or set WEKNORA_API_KEY / the repo .env)")
    secret = resolve_secret(args.api_key, args.api_secret, args.keys_file)
    if not secret:
        print(
            "no api_secret found for this api_key: pass --api-secret, set "
            "WEKNORA_API_SECRET (.env), or add the pair to keys.json",
            file=sys.stderr,
        )
        return 2

    body = b""
    if args.json_body:
        try:
            if args.json_body.startswith("@"):
                with open(args.json_body[1:], "rb") as handle:
                    body = handle.read()
            else:
                body = args.json_body.encode("utf-8")
        except OSError as exc:
            print(f"cannot read the body: {exc}", file=sys.stderr)
            return 2

    headers = build_headers(args.method, args.path, args.api_key, secret)
    for raw in args.header:
        name, value = _split_header(raw)
        if not name or not value:
            print(f"ignoring malformed --header {raw!r}", file=sys.stderr)
            continue
        headers[name] = value

    if args.dry_run:
        print(json.dumps(headers, indent=2, ensure_ascii=False))
        print(
            f"# payload signed: {signature_payload(args.method, args.path)}",
            file=sys.stderr,
        )
        return 0

    url = args.base.rstrip("/") + args.path
    request = urllib.request.Request(
        url, data=body or None, headers=headers, method=args.method.upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as resp:
            print(resp.status)
            print(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        print(exc.code)
        print(exc.read().decode("utf-8", "replace"))
        return 1
    except urllib.error.URLError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

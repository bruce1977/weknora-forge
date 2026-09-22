#!/usr/bin/env python3
"""Send a request carrying the Forge HMAC second factor.

The scheme is deliberately tiny::

    payload   = HTTP_METHOD + HTTP_FULL_PATH      e.g. "POST/api/v2/publish?dry_run=1"
    signature = hex(HMAC_SHA256(api_key, payload))   ->  X-Forge-Signature

There is no timestamp, no nonce and no body digest: the WeKnora API key IS the signing
key, so no extra secret has to be distributed. Freshness comes from the signature being
single use for state-changing methods (POST/PUT/PATCH/DELETE) - simply re-sending the
same request is rejected as a replay.

Usage::

    python scripts/hmac_request.py --base http://localhost:8000 --api-key sk-xxxxx \
        POST /api/v2/publish --json '{"kb_id":"kb-1","title":"t","content":"c"}'

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
import sys
import urllib.error
import urllib.request


def signature_payload(method: str, path: str) -> str:
    """METHOD + FULL_PATH, concatenated without a separator."""
    return f"{method.upper()}{path}"


def build_signature(method: str, path: str, api_key: str) -> str:
    payload = signature_payload(method, path).encode("utf-8")
    return hmac.new(api_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _split_header(raw: str) -> tuple:
    """Accept both "Name: value" and "Name=value"."""
    for separator in ("=", ":"):
        name, found, value = raw.partition(separator)
        if found:
            return name.strip(), value.strip()
    return "", ""


def build_headers(method: str, path: str, api_key: str, label: str = "") -> dict:
    headers = {
        "X-Forge-Signature": build_signature(method, path, api_key),
        "Content-Type": "application/json",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def main() -> int:
    parser = argparse.ArgumentParser(description="Forge HMAC signed request helper")
    parser.add_argument("method", help="GET / POST / PUT / PATCH / DELETE ...")
    parser.add_argument("path", help="Path starting with /, including the query string if any")
    parser.add_argument("--base", default="http://localhost:8000", help="Service base URL")
    parser.add_argument("--api-key", required=True, help="WeKnora API key - also the HMAC signing key")
    parser.add_argument(
        "--json",
        dest="json_body",
        default="",
        help="JSON string, or @file to read the body from a file",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the signature headers only")
    parser.add_argument("--timeout", type=float, default=120.0, help="Request timeout in seconds")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Extra request header, repeatable. Handy to replay what a proxy sends, e.g. "
        "--header 'X-Forwarded-Proto: https' --header 'CF-Connecting-IP: 198.51.100.9'",
    )
    args = parser.parse_args()

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

    headers = build_headers(args.method, args.path, args.api_key)
    for raw in args.header:
        name, value = _split_header(raw)
        if not name or not value:
            print(f"ignoring malformed --header {raw!r}", file=sys.stderr)
            continue
        headers[name] = value

    if args.dry_run:
        print(json.dumps(headers, indent=2, ensure_ascii=False))
        print(f"# payload signed: {signature_payload(args.method, args.path)}", file=sys.stderr)
        return 0

    url = args.base.rstrip("/") + args.path
    request = urllib.request.Request(url, data=body or None, headers=headers, method=args.method.upper())
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

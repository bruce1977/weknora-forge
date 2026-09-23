#!/usr/bin/env python3
"""Generate the X-Forge-Signature header for Forge v2 requests.

Forge's second factor is a single header, X-Forge-Signature::

    payload   = HTTP_METHOD + HTTP_FULL_PATH        e.g. "POST/api/v2/publish?dry_run=1"
    signature = hex(HMAC_SHA256(api_key, payload))

The WeKnora API key (X-API-Key) IS the signing key, so no extra secret is distributed.
There is no timestamp/nonce: freshness comes from the signature being single use for
state-changing methods (POST/PUT/PATCH/DELETE) - a replay is rejected. So generate a
FRESH signature for every mutating call; GET signatures may be reused.

The signed payload uses the raw path + raw query exactly as it travels on the wire
(percent-encoding included) - Forge never normalises either, so an HTTPS-terminating proxy
in front cannot break the signature.

Usage::

    python scripts/gen_forge_signature.py --method POST --path /api/v2/publish \\
        --api-key sk-xxxxx

    # include a query string (pass it as part of --path, or via --query)
    python scripts/gen_forge_signature.py --method GET \\
        --path /api/v2/knowledge/search --query "metas_query=level%20%3E%3D%203" \\
        --api-key sk-xxxxx --curl

    # print a ready-to-run curl command (handy for Postman / job runners)
    python scripts/gen_forge_signature.py --method POST --path /api/v2/publish \\
        --api-key sk-xxxxx --curl --json '{"kb_id":"kb-1"}'
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys


def signature_payload(method: str, full_path: str) -> str:
    """METHOD + FULL_PATH, concatenated without a separator (matches app/security.py)."""
    return f"{method.upper()}{full_path}"


def compute_signature(secret: str, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _build_full_path(path: str, query: str) -> str:
    if query and "?" not in path:
        return f"{path}?{query}"
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the Forge X-Forge-Signature header for a v2 request."
    )
    parser.add_argument(
        "--method", required=True, help="HTTP method, e.g. GET / POST / PUT"
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Request path starting with /; include the query string here or via --query",
    )
    parser.add_argument(
        "--api-key", required=True, help="WeKnora API key (also the HMAC secret)"
    )
    parser.add_argument(
        "--query", default="", help="Raw query string (without the leading ?)"
    )
    parser.add_argument(
        "--json",
        dest="json_body",
        default="",
        help="Optional JSON body, or @file to read it from a file (only printed in --curl)",
    )
    parser.add_argument(
        "--curl",
        action="store_true",
        help="Also print a ready-to-run curl command with the signed headers",
    )
    args = parser.parse_args()

    full_path = _build_full_path(args.path, args.query)
    sig = compute_signature(args.api_key, signature_payload(args.method, full_path))
    payload = signature_payload(args.method, full_path)

    print("# Headers to send")
    print(f"X-API-Key: {args.api_key}")
    print(f"X-Forge-Signature: {sig}")
    print()
    print(f"# Signed payload: {payload}")

    if args.curl:
        print()
        print("# curl command")
        cmd = [
            f"curl -X {args.method.upper()} '{full_path}' \\",
            f"  -H 'X-API-Key: {args.api_key}' \\",
            f"  -H 'X-Forge-Signature: {sig}'",
        ]
        if args.json_body:
            cmd.append(" \\")
            cmd.append("  -H 'Content-Type: application/json' \\")
            cmd.append(f"  -d '{args.json_body}'")
        print("\n".join(cmd))
    return 0


if __name__ == "__main__":
    sys.exit(main())

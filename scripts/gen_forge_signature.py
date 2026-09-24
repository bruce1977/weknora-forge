#!/usr/bin/env python3
"""Generate the X-Forge-Signature header for Forge v2 requests.

Forge's second factor is a single header, X-Forge-Signature::

    payload   = HTTP_METHOD + HTTP_FULL_PATH        e.g. "POST/api/v2/publish?dry_run=1"
    signature = hex(HMAC_SHA256(api_secret, payload))

``api_secret`` is the secret paired with the WeKnora API key (X-API-Key). It is
resolved in this order:

1. ``--api-secret``
2. ``$WEKNORA_API_SECRET`` (when it belongs to ``--api-key`` / ``$WEKNORA_API_KEY``)
3. the ``keys.json`` entry whose ``api_key`` matches (``--keys-file``, else next to
   ``config.json`` via ``$FORGE_CONFIG``, else ``data/keys.json``)

``--api-key`` defaults to ``$WEKNORA_API_KEY``.  When the environment is not
exported, both values are read from the repository ``.env`` first, so local
testing works right after ``cp example.env .env``.

There is no timestamp/nonce: freshness comes from the signature being single use for
state-changing methods (POST/PUT/PATCH/DELETE) - a replay is rejected. So generate a
FRESH signature for every mutating call; GET signatures may be reused.

The signed payload uses the raw path + raw query exactly as it travels on the wire
(percent-encoding included) - Forge never normalises either, so an HTTPS-terminating proxy
in front cannot break the signature.

Usage::

    python scripts/gen_forge_signature.py --method POST --path /api/v2/publish \\
        --api-key sk-xxxxx --api-secret <secret>

    # secret falls back to $WEKNORA_API_SECRET / keys.json when omitted
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
import json
import os
import sys
from pathlib import Path
from typing import Dict


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


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


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


def main() -> int:
    _fill_from_env_file()
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

    full_path = _build_full_path(args.path, args.query)
    sig = compute_signature(secret, signature_payload(args.method, full_path))
    payload = signature_payload(args.method, full_path)

    print("# Headers to send")
    print(f"X-API-Key: {args.api_key}")
    print(f"X-Forge-Signature: {sig}")
    print()
    print(f"# Signed payload: {payload}")
    print(f"# Signed with api_secret: {_mask(secret)}")

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

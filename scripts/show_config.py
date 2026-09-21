"""Show the effective configuration (secrets masked).

    python scripts/show_config.py                 # print the loaded config
    python scripts/show_config.py --template      # print the documented defaults
    FORGE_CONFIG=/etc/forge/config.json python scripts/show_config.py

Useful to verify that ${ENV} placeholders resolved the way you expected before
restarting the service.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

SECRET_HINTS = ("password", "secret", "token", "key", "dsn")


def mask(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("***" if any(h in k.lower() for h in SECRET_HINTS) and v else mask(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask(v) for v in data]
    return data


def main() -> int:
    from app.config import config_template, default_config_path, get_config

    if "--template" in sys.argv:
        print(json.dumps(config_template(), indent=2, ensure_ascii=False))
        return 0

    config = get_config()
    payload: Dict[str, Any] = {
        "config_path": str(default_config_path()),
        "resolved": json.loads(config.model_dump_json()),
    }
    print(json.dumps(mask(payload), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

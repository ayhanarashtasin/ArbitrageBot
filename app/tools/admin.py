"""Configure and use the existing local admin API without printing its token.

    python -m app.tools.admin setup
    python -m app.tools.admin check
    python -m app.tools.admin pause --reason "maintenance"
    python -m app.tools.admin resume --reason "maintenance complete"

setup preserves an existing token; restart the bot after creating one.
check submits an intentionally invalid action body: authentication succeeds but validation prevents any action.
"""

import argparse
import json
import secrets
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key

from app.core.trading_config import TradingSettings

ENDPOINTS = {"pause": "pause-new-entries", "resume": "resume-entries",
             "flatten": "emergency-flatten", "clear-flatten": "clear-emergency-flatten"}


def setup(env_file: Path) -> dict:
    if not env_file.is_file():
        raise ValueError("The project .env must already exist")
    created = not bool(dotenv_values(env_file).get("ADMIN_API_TOKEN"))
    if created:
        set_key(str(env_file), "ADMIN_API_TOKEN", secrets.token_urlsafe(32))
    return {"configured": True, "created": created, "restart_required": created,
            "env_file": str(env_file.resolve())}


def main(argv=None, *, transport=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "check", *ENDPOINTS])
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    if args.command in ENDPOINTS and (not args.reason or not 3 <= len(args.reason) <= 300):
        parser.error("actions require --reason with 3-300 characters")
    if not 1 <= len(args.actor) <= 64:
        parser.error("--actor must have 1-64 characters")
    try:
        if args.command == "setup":
            print(json.dumps(setup(Path(".env"))))
            return 0
        token = TradingSettings().admin_api_token
        if not token:
            print("Admin token is unset; run python -m app.tools.admin setup and restart the bot")
            return 1
        endpoint = ENDPOINTS.get(args.command, "pause-new-entries")
        body = {} if args.command == "check" else {"actor": args.actor, "reason": args.reason}
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False,
                          transport=transport) as client:
            response = client.post(f"/admin/{endpoint}", headers={"X-Admin-Token": token}, json=body)
        if args.command == "check":
            ok = response.status_code == 422
            print(json.dumps({"authenticated": ok, "action_executed": False,
                              "http_status": response.status_code}))
            return 0 if ok else 1
        response.raise_for_status()
        print(json.dumps(response.json()))
        return 0
    except (OSError, ValueError, httpx.HTTPError) as exc:
        # Never include a request, response body, environment value or exception message containing secrets.
        print(json.dumps({"error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

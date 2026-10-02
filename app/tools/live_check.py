"""Live readiness check. READ-ONLY: it never places, changes or cancels anything.

    python -m app.tools.live_check

For every venue with API credentials in .env it checks that the key works, the clock is in sync, the account is in
one-way mode with the margin mode the bot needs, and there is USDT to trade with. It also lists what the live gate
still needs (LIVE_TRADING_ENABLED, the confirmation phrase, the REQUIRED_BEFORE_LIVE values). Exit code 0 means at least
two venues are ready AND the gate is complete; the bot can then be started with RUN_MODE=live.
"""

import asyncio
import sys

from app.core.trading_config import LIVE_CONFIRMATION_PHRASE, TradingSettings
from app.exchanges.private.credentials import LiveCredentials
from app.exchanges.private.registry import PRIVATE_CLIENTS, build_private_clients


async def main() -> int:
    credentials = LiveCredentials()
    clients = build_private_clients(credentials)
    print("Live readiness check (read-only: nothing is traded or changed)\n")
    missing = [name for name in PRIVATE_CLIENTS if name not in clients]
    if missing:
        print(f"No credentials (not traded live): {', '.join(missing)}\n")
    ready = []
    results = await asyncio.gather(*(c.readiness() for c in clients.values()), return_exceptions=True)
    for (name, client), result in zip(clients.items(), results):
        if isinstance(result, BaseException):
            print(f"[FAIL] {name}: {type(result).__name__}: {result}\n")
            continue
        print(f"[{'READY' if result.ok else 'FAIL '}] {name}")
        for check in result.checks:
            print(f"    {'ok  ' if check.ok else 'FIX '} {check.name}: {check.detail}")
        print()
        if result.ok:
            ready.append(name)
        await client.close()

    settings = TradingSettings()
    gate = []
    if not settings.live_trading_enabled:
        gate.append("LIVE_TRADING_ENABLED=true")
    if settings.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
        gate.append(f"LIVE_TRADING_CONFIRMATION={LIVE_CONFIRMATION_PHRASE}")
    gate += [f"{name}=<your value>" for name in settings.missing_required()]
    print(f"Venues ready: {len(ready)} ({', '.join(ready) or 'none'}); two are needed.")
    if gate:
        print("The live gate still needs, in .env:\n    " + "\n    ".join(gate))
    else:
        print("The live gate is complete.")
    ok = len(ready) >= 2 and not gate
    print("\nRESULT: " + ("ready - start the bot with RUN_MODE=live" if ok else "not ready yet - fix the items above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

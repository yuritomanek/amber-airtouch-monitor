#!/usr/bin/env python3
"""
test_connection.py

A one-off connectivity test for the energy price automation project.
It checks the two ends of the system independently:

  1. Amber Electric API  -> can we read the current price?
  2. AirTouch 2+ (LAN)    -> can we connect and read the AC status,
                             and (optionally) turn it off and back on?

Run this on a machine on your home network (your Raspberry Pi is ideal),
NOT in a cloud sandbox, because the AirTouch is only reachable on your LAN.

Usage:
  python3 test_connection.py                 # read-only: price + AC status
  python3 test_connection.py --cycle         # also test turning the AC off then on
  python3 test_connection.py --amber-only    # only test the Amber API
  python3 test_connection.py --airtouch-only # only test the AirTouch

Config comes from environment variables (see .env.example):
  AMBER_API_TOKEN   your Amber API token from app.amber.com.au/developers
  AMBER_SITE_ID     (optional) skip site lookup if you already know it
  AIRTOUCH_HOST     the AirTouch IP, e.g. 192.168.1.100
"""

import argparse
import asyncio
import logging
import os
import sys

# Example only. Set your AirTouch's real IP via AIRTOUCH_HOST in .env.
AIRTOUCH_DEFAULT_HOST = "192.168.1.100"

# The airtouch2 library logs a noisy WARNING with a stack trace when the unit
# sends a message type it does not recognise (for example 0x27). This is benign
# and recoverable, so quiet it. Set AIRTOUCH_LOG_LEVEL=WARNING to see it again.
logging.getLogger("airtouch2").setLevel(
    getattr(logging, os.environ.get("AIRTOUCH_LOG_LEVEL", "ERROR").upper(), logging.ERROR)
)


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so you do not have to source the file manually.

    Reads KEY=value lines from a .env next to this script (or the given path)
    and sets them in os.environ if not already set. No dependency required.
    """
    # Look next to this script first, then the current directory.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, path), path]
    for env_path in candidates:
        if not os.path.isfile(env_path):
            continue
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                # Strip surrounding quotes and inline whitespace.
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        return


# --------------------------------------------------------------------------
# Amber Electric
# --------------------------------------------------------------------------
def test_amber(token: str, site_id: str | None) -> int:
    """Read current prices from Amber. Returns 0 on success, 1 on failure."""
    try:
        import amberelectric
        from amberelectric.models.channel import ChannelType
        from amberelectric.rest import ApiException
    except ImportError:
        print("  [amber] amberelectric not installed. Run: pip install amberelectric")
        return 1

    print("== Amber Electric ==")
    config = amberelectric.Configuration(access_token=token)

    try:
        with amberelectric.ApiClient(config) as api_client:
            api = amberelectric.AmberApi(api_client)

            # Resolve the site id if not provided.
            if not site_id:
                sites = api.get_sites()
                if not sites:
                    print("  [amber] No sites returned for this account.")
                    return 1
                # Prefer an active site if the field is present.
                site = sites[0]
                site_id = site.id
                print(f"  [amber] Using site {site_id} (NMI {getattr(site, 'nmi', '?')})")
            else:
                print(f"  [amber] Using configured site {site_id}")

            # Current interval plus one forecast interval.
            intervals = api.get_current_prices(site_id, next=1)

            # The library wraps each item in an Interval whose real data
            # (CurrentInterval / ForecastInterval / ActualInterval) lives in
            # .actual_instance. Unwrap it, tolerating either shape.
            unwrapped = [getattr(i, "actual_instance", i) for i in intervals]

            general = [
                i for i in unwrapped
                if getattr(i, "channel_type", None) == ChannelType.GENERAL
            ]
            if not general:
                print("  [amber] No 'general' channel intervals returned.")
                return 1

            for i in general:
                per_kwh = getattr(i, "per_kwh", None)
                dollars = (per_kwh / 100.0) if per_kwh is not None else None
                itype = getattr(i, "type", "?")
                desc = getattr(i, "descriptor", "?")
                spike = getattr(i, "spike_status", "?")
                start = getattr(i, "start_time", "?")
                price_str = f"{per_kwh:.2f} c/kWh (${dollars:.4f}/kWh)" if per_kwh is not None else "?"
                print(f"  [amber] {itype:<8} {start}  {price_str}  descriptor={desc}  spike={spike}")

        print("  [amber] OK")
        return 0

    except ApiException as e:
        print(f"  [amber] API error (status {e.status}): {e.reason}")
        print("          Check that your token is valid and has not expired.")
        return 1
    except Exception as e:  # noqa: BLE001 - this is a diagnostic tool
        print(f"  [amber] Unexpected error: {type(e).__name__}: {e}")
        return 1


# --------------------------------------------------------------------------
# AirTouch 2+
# --------------------------------------------------------------------------
async def test_airtouch(host: str, cycle: bool) -> int:
    """Connect to the AirTouch, print status, optionally cycle power."""
    try:
        from airtouch2.at2plus import At2PlusClient
    except ImportError:
        print("  [airtouch] airtouch2 not installed. Run: pip install airtouch2")
        return 1

    print("== AirTouch 2+ ==")
    print(f"  [airtouch] Connecting to {host}:9200 ...")
    client = At2PlusClient(host)

    try:
        connected = await client.connect()
        if not connected:
            print("  [airtouch] Could not connect. Check the IP and that you are on the same LAN.")
            return 1

        # Start the background read loop, then wait for the AC to be discovered.
        client.run()
        try:
            await client.wait_for_ac(timeout=10)
        except asyncio.TimeoutError:
            print("  [airtouch] Connected but no AC unit was discovered within 10s.")
            await client.stop()
            return 1

        if not client.aircons_by_id:
            print("  [airtouch] No aircons found.")
            await client.stop()
            return 1

        # Take the first AC unit (most homes have one head unit).
        ac_id, ac = next(iter(client.aircons_by_id.items()))
        # Wait until its abilities are known so mode info is valid.
        try:
            await asyncio.wait_for(ac.wait_until_ready(), timeout=5)
        except asyncio.TimeoutError:
            pass  # status is still readable even if ability metadata is slow

        s = ac.status
        print(f"  [airtouch] AC id={ac_id}")
        print(f"  [airtouch] power={s.power}  mode={s.mode}  fan={s.fan_speed}")
        print(f"  [airtouch] temp={s.temperature}  setpoint={s.set_point}")
        print(f"  [airtouch] is_on() -> {ac.is_on()}")

        if cycle:
            if not ac.is_on():
                print("  [airtouch] --cycle requested but AC is already OFF. Skipping to avoid turning it on.")
            else:
                print("  [airtouch] --cycle: turning AC OFF ...")
                await ac.turn_off()
                await asyncio.sleep(5)
                print(f"  [airtouch] is_on() -> {ac.is_on()} (expected False)")
                print("  [airtouch] turning AC back ON ...")
                await ac.turn_on()
                await asyncio.sleep(5)
                print(f"  [airtouch] is_on() -> {ac.is_on()} (expected True)")

        await client.stop()
        print("  [airtouch] OK")
        return 0

    except Exception as e:  # noqa: BLE001 - diagnostic tool
        print(f"  [airtouch] Unexpected error: {type(e).__name__}: {e}")
        try:
            await client.stop()
        except Exception:
            pass
        return 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    load_dotenv()  # auto-load .env so AMBER_API_TOKEN etc. are available

    parser = argparse.ArgumentParser(description="Test Amber + AirTouch connectivity.")
    parser.add_argument("--cycle", action="store_true",
                        help="Turn the AC off then back on as a control test (only if currently on).")
    parser.add_argument("--amber-only", action="store_true", help="Only test the Amber API.")
    parser.add_argument("--airtouch-only", action="store_true", help="Only test the AirTouch.")
    parser.add_argument("--host", default=os.environ.get("AIRTOUCH_HOST", AIRTOUCH_DEFAULT_HOST),
                        help=f"AirTouch IP (default {AIRTOUCH_DEFAULT_HOST}).")
    args = parser.parse_args()

    run_amber = not args.airtouch_only
    run_airtouch = not args.amber_only

    results = []

    if run_amber:
        token = os.environ.get("AMBER_API_TOKEN")
        if not token:
            print("== Amber Electric ==")
            print("  [amber] AMBER_API_TOKEN is not set. Skipping. See .env.example.")
            results.append(1)
        else:
            results.append(test_amber(token, os.environ.get("AMBER_SITE_ID")))
        print()

    if run_airtouch:
        results.append(asyncio.run(test_airtouch(args.host, args.cycle)))
        print()

    ok = all(r == 0 for r in results) and len(results) > 0
    print("RESULT:", "ALL OK" if ok else "one or more checks failed (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

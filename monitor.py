#!/usr/bin/env python3
"""
monitor.py

Watches Amber Electric prices and protects against price spikes by turning the
AirTouch 2+ off when power gets expensive, and back on when it is cheap again.

Logic (edge-triggered with hysteresis and a cooldown):

  state = NORMAL
      If price >= UPPER_THRESHOLD_C  OR  Amber flags a SPIKE:
          turn the AC OFF (only if it is currently on), remember that WE did it,
          move to state = CURTAILED.

  state = CURTAILED
      If price <= LOWER_THRESHOLD_C  AND  Amber is not flagging a spike:
          if WE were the ones who turned it off, turn it back ON (previous mode
          preserved unless RESTORE_MODE is set),
          move to state = NORMAL.

Safety features:
  - Only turns the AC back ON if this program turned it OFF. If your AC was
    already off when a spike hit, it will not switch it on later.
  - DRY_RUN mode (default ON) logs every decision without touching the AC, so
    you can watch it for a day before trusting it.
  - Cooldown prevents rapid on/off cycling (compressor protection).
  - If the Amber API is unreachable, it does nothing and logs a warning.
  - State is persisted to disk, so a restart does not lose track or double-fire.

Configuration is read from environment variables (and a .env file). See
.env.example for the full list.

Run:
  python3 monitor.py
"""

import asyncio
import ipaddress
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Example only. Set your AirTouch's real IP via AIRTOUCH_HOST in .env.
AIRTOUCH_DEFAULT_HOST = "192.168.1.100"


# --------------------------------------------------------------------------
# Minimal .env loader (no dependency required)
# --------------------------------------------------------------------------
def load_dotenv(path: str = ".env") -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for env_path in (os.path.join(here, path), path):
        if not os.path.isfile(env_path):
            continue
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        return


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.amber_token = os.environ.get("AMBER_API_TOKEN")
        self.amber_site_id = os.environ.get("AMBER_SITE_ID") or None
        self.airtouch_host = os.environ.get("AIRTOUCH_HOST", AIRTOUCH_DEFAULT_HOST)

        self.upper_threshold_c = float(os.environ.get("UPPER_THRESHOLD_C", "100"))
        self.lower_threshold_c = float(os.environ.get("LOWER_THRESHOLD_C", "80"))
        self.trigger_on_spike = _get_bool("TRIGGER_ON_SPIKE", True)

        # Price resolution in minutes (5 or 30). Default 5 to match what the
        # Amber app shows and what 5-minute settlement actually charges. The
        # account default (often 30) can diverge a lot during volatile periods.
        self.resolution_minutes = int(os.environ.get("PRICE_RESOLUTION_MINUTES", "5"))

        self.poll_interval_s = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
        self.cooldown_minutes = float(os.environ.get("COOLDOWN_MINUTES", "15"))
        self.dry_run = _get_bool("DRY_RUN", True)

        # AirTouch connection resilience. The console can drop off wifi. These
        # retry a flaky connection within a single action, and poll faster while
        # an action is needed but failing, so the monitor acts the moment the
        # AirTouch comes back instead of waiting a full poll interval.
        self.airtouch_connect_retries = int(os.environ.get("AIRTOUCH_CONNECT_RETRIES", "3"))
        self.airtouch_retry_delay_s = float(os.environ.get("AIRTOUCH_RETRY_DELAY_SECONDS", "5"))
        self.failed_retry_seconds = int(os.environ.get("FAILED_RETRY_SECONDS", "30"))

        # Auto-discovery: if the configured IP stops answering (for example the
        # AirTouch's DHCP lease changed), scan the local subnet for it on port
        # 9200 and use the address found. The discovered IP is remembered.
        self.airtouch_auto_discover = _get_bool("AIRTOUCH_AUTO_DISCOVER", True)
        # CIDR to scan, e.g. "192.168.0.0/23". Blank = derive a /24 from
        # AIRTOUCH_HOST. Set this if your LAN is larger than a /24.
        self.airtouch_discover_cidr = os.environ.get("AIRTOUCH_DISCOVER_CIDR", "").strip()

        # Forecast-aware restore: before turning heating back on, look at the
        # next few forecast intervals. If any is about to be expensive or is
        # flagged as a (potential) spike, keep the heating off for now.
        self.forecast_lookahead = int(os.environ.get("FORECAST_LOOKAHEAD", "3"))
        self.forecast_restore_guard = _get_bool("FORECAST_RESTORE_GUARD", True)

        # Mode to set when restoring. Empty (default) means leave the AC's mode
        # untouched, so it returns to whatever it was (for example AUTO). Set to
        # HEAT, COOL, AUTO, DRY or FAN only if you want to force a mode.
        self.restore_mode = os.environ.get("RESTORE_MODE", "").strip().upper()

        self.state_file = os.environ.get("STATE_FILE", "monitor_state.json")
        self.log_file = os.environ.get("LOG_FILE", "monitor.log")

    def validate(self) -> list[str]:
        errors = []
        if not self.amber_token:
            errors.append("AMBER_API_TOKEN is not set.")
        if self.lower_threshold_c >= self.upper_threshold_c:
            errors.append(
                f"LOWER_THRESHOLD_C ({self.lower_threshold_c}) must be below "
                f"UPPER_THRESHOLD_C ({self.upper_threshold_c}) for hysteresis."
            )
        return errors


# --------------------------------------------------------------------------
# State persistence
# --------------------------------------------------------------------------
STATE_NORMAL = "normal"
STATE_CURTAILED = "curtailed"


def load_state(path: str) -> dict:
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logging.warning("Could not read state file %s, starting fresh.", path)
    return {"state": STATE_NORMAL, "we_turned_off": False, "last_action_ts": None}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logging.error("Could not write state file %s: %s", path, e)


# --------------------------------------------------------------------------
# Spike-status helpers (the string looks like "SpikeStatus.SPIKE")
# --------------------------------------------------------------------------
def is_spike(spike_str: str) -> bool:
    return spike_str.lower().endswith("spike")


def is_potential_spike(spike_str: str) -> bool:
    return spike_str.lower().endswith("potential")


# --------------------------------------------------------------------------
# Amber: current general-channel price plus upcoming forecast intervals
# --------------------------------------------------------------------------
class AmberData:
    def __init__(self, price: float, descriptor: str, spike: str,
                 forecasts: list[tuple[float, str]],
                 spot: float | None = None, estimate: bool | None = None,
                 start: str | None = None, duration: int | None = None) -> None:
        self.price = price            # current per-kWh price in cents (total retail)
        self.descriptor = descriptor  # current PriceDescriptor
        self.spike = spike            # current SpikeStatus
        self.forecasts = forecasts    # list of (price_cents, spike_status), in time order
        self.spot = spot              # wholesale spot component, for diagnostics
        self.estimate = estimate      # True if this interval is still an estimate
        self.start = start            # interval start time (string)
        self.duration = duration      # interval length in minutes (5 or 30)


def get_amber_data(cfg: Config) -> AmberData | None:
    """Return current price plus the next forecast intervals for the general
    channel, or None on any failure (so the caller can fail safe)."""
    try:
        import amberelectric
        from amberelectric.models.channel import ChannelType
        from amberelectric.rest import ApiException
    except ImportError:
        logging.error("amberelectric not installed.")
        return None

    config = amberelectric.Configuration(access_token=cfg.amber_token)
    try:
        with amberelectric.ApiClient(config) as api_client:
            api = amberelectric.AmberApi(api_client)

            site_id = cfg.amber_site_id
            if not site_id:
                sites = api.get_sites()
                if not sites:
                    logging.error("Amber returned no sites.")
                    return None
                site_id = sites[0].id

            want = max(0, cfg.forecast_lookahead)
            intervals = api.get_current_prices(
                site_id, next=want, resolution=cfg.resolution_minutes)
            unwrapped = [getattr(i, "actual_instance", i) for i in intervals]
            general = [
                i for i in unwrapped
                if getattr(i, "channel_type", None) == ChannelType.GENERAL
            ]
            if not general:
                logging.warning("No general channel interval in Amber response.")
                return None

            current = None
            forecasts: list[tuple[float, str]] = []
            for i in general:
                itype = str(getattr(i, "type", ""))
                if "Forecast" in itype:
                    forecasts.append((float(getattr(i, "per_kwh")),
                                      str(getattr(i, "spike_status", "?"))))
                elif current is None:  # CurrentInterval (or ActualInterval)
                    current = i

            if current is None:
                current = general[0]

            spot_val = getattr(current, "spot_per_kwh", None)
            return AmberData(
                price=float(getattr(current, "per_kwh")),
                descriptor=str(getattr(current, "descriptor", "?")),
                spike=str(getattr(current, "spike_status", "?")),
                forecasts=forecasts,
                spot=float(spot_val) if spot_val is not None else None,
                estimate=getattr(current, "estimate", None),
                start=str(getattr(current, "start_time", "?")),
                duration=getattr(current, "duration", None),
            )
    except ApiException as e:
        logging.warning("Amber API error (status %s): %s", e.status, e.reason)
        return None
    except Exception as e:  # noqa: BLE001 - fail safe on any error
        logging.warning("Amber unexpected error: %s: %s", type(e).__name__, e)
        return None


def forecast_is_risky(cfg: Config, forecasts: list[tuple[float, str]]) -> str | None:
    """If any of the next forecast intervals is expensive or (potentially)
    spiking, return a human reason string. Otherwise return None."""
    for idx, (price, spike) in enumerate(forecasts[: cfg.forecast_lookahead], start=1):
        if price >= cfg.upper_threshold_c:
            return f"forecast interval +{idx} is {price:.2f} c/kWh (>= {cfg.upper_threshold_c})"
        if is_spike(spike):
            return f"forecast interval +{idx} flagged SPIKE"
        if is_potential_spike(spike):
            return f"forecast interval +{idx} flagged POTENTIAL spike"
    return None


# --------------------------------------------------------------------------
# AirTouch: discovery (find the unit if its IP changed)
# --------------------------------------------------------------------------
AIRTOUCH_PORT = 9200


async def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def default_cidr_for(host: str) -> str:
    """Best-effort /24 containing the host, used when no CIDR is configured."""
    try:
        return str(ipaddress.ip_network(f"{host}/24", strict=False))
    except ValueError:
        return f"{host.rsplit('.', 1)[0]}.0/24"


async def discover_airtouch(cidr: str, exclude: str | None = None,
                            timeout: float = 1.0, concurrency: int = 128) -> list[str]:
    """Scan every host address in `cidr` for something listening on the AirTouch
    port. Handles /24, /23 or any prefix. Returns candidate IPs (the configured
    one excluded so new addresses are tried first). On a home LAN, port 9200 is
    almost always just the AirTouch."""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        logging.error("Invalid discovery CIDR '%s', skipping discovery.", cidr)
        return []

    hosts = [str(ip) for ip in network.hosts() if str(ip) != exclude]
    found: list[str] = []
    sem = asyncio.Semaphore(concurrency)

    async def check(h: str) -> None:
        async with sem:
            if await _port_open(h, AIRTOUCH_PORT, timeout):
                found.append(h)

    await asyncio.gather(*(check(h) for h in hosts))
    return found


# --------------------------------------------------------------------------
# AirTouch: perform an off/on action, returning (was_on, now_on, host_used)
# --------------------------------------------------------------------------
async def _try_host(host: str, turn_on: bool, mode: str | None, dry_run: bool) -> tuple[bool, bool]:
    """One attempt against a single host. Raises on any failure."""
    from airtouch2.at2plus import At2PlusClient
    from airtouch2.protocol.at2plus.enums import AcSetMode

    client = At2PlusClient(host)
    try:
        if not await client.connect():
            raise ConnectionError(f"could not connect to AirTouch at {host}")
        client.run()
        await client.wait_for_ac(timeout=15)
        if not client.aircons_by_id:
            raise RuntimeError("no aircons discovered")

        ac = next(iter(client.aircons_by_id.values()))
        was_on = ac.is_on()

        if dry_run:
            mode_note = f" (force {mode} mode)" if (turn_on and mode) else ""
            logging.info("DRY RUN: would %s the AC%s (currently %s).",
                         "turn ON" if turn_on else "turn OFF",
                         mode_note, "ON" if was_on else "OFF")
            await client.stop()
            return was_on, was_on

        if turn_on:
            if mode:
                ac_mode = getattr(AcSetMode, mode, None)
                if ac_mode is None:
                    logging.warning("Unknown RESTORE_MODE '%s', leaving mode unchanged.", mode)
                else:
                    try:
                        await asyncio.wait_for(ac.wait_until_ready(), timeout=10)
                        await ac.set_mode(ac_mode)
                        await asyncio.sleep(1)
                    except asyncio.TimeoutError:
                        logging.warning("AC ability not ready, setting power without mode change.")
            await ac.turn_on()
        else:
            await ac.turn_off()

        await asyncio.sleep(3)
        now_on = ac.is_on()
        await client.stop()
        return was_on, now_on
    except Exception:
        try:
            await client.stop()
        except Exception:
            pass
        raise


async def airtouch_action(host: str, turn_on: bool, mode: str | None, dry_run: bool,
                          retries: int = 3, delay: float = 5.0,
                          auto_discover: bool = True, cidr: str | None = None
                          ) -> tuple[bool, bool, str]:
    """Turn the AC on or off, with connection retries and optional LAN discovery
    if the configured IP stops answering. Returns (was_on, now_on, host_used)."""
    candidates = [host]
    discovered = False
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        for cand in list(candidates):
            try:
                was_on, now_on = await _try_host(cand, turn_on, mode, dry_run)
                return was_on, now_on, cand
            except Exception as e:  # noqa: BLE001
                last_err = e

        # Configured host (and any candidates) failed this round.
        if auto_discover and not discovered:
            scan_cidr = cidr or default_cidr_for(host)
            logging.warning("AirTouch unreachable at %s. Scanning %s for it...", host, scan_cidr)
            try:
                found = await discover_airtouch(scan_cidr, exclude=host)
            except Exception as e:  # noqa: BLE001
                found = []
                last_err = e
            discovered = True
            if found:
                logging.warning("Found AirTouch candidate(s) on the network: %s", ", ".join(found))
                candidates = found + [host]
                continue  # retry immediately with discovered addresses
            else:
                logging.warning("Discovery found nothing on port %d in %s.", AIRTOUCH_PORT, scan_cidr)

        if attempt < retries:
            logging.warning("AirTouch attempt %d/%d failed (%s), retrying in %.0fs...",
                            attempt, retries, last_err, delay)
            await asyncio.sleep(delay)

    raise last_err if last_err else ConnectionError(f"could not reach AirTouch (tried {candidates})")


def do_airtouch(host: str, turn_on: bool, mode: str | None, dry_run: bool,
                retries: int = 3, delay: float = 5.0,
                auto_discover: bool = True, cidr: str | None = None) -> tuple[bool, bool, str]:
    return asyncio.run(airtouch_action(host, turn_on, mode, dry_run, retries, delay,
                                       auto_discover, cidr))


# --------------------------------------------------------------------------
# Decision helpers
# --------------------------------------------------------------------------
def cooldown_active(state: dict, cooldown_minutes: float) -> bool:
    ts = state.get("last_action_ts")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts)
    except ValueError:
        return False
    elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
    return elapsed_min < cooldown_minutes


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
_running = True


def _handle_signal(signum, frame):  # noqa: ARG001
    global _running
    logging.info("Received signal %s, shutting down after this cycle.", signum)
    _running = False


def evaluate_once(cfg: Config, state: dict) -> dict:
    """Run one decision cycle. Returns the (possibly updated) state."""
    data = get_amber_data(cfg)
    if data is None:
        logging.warning("No price this cycle, taking no action (fail safe).")
        return state

    price = data.price
    spike_now = cfg.trigger_on_spike and is_spike(data.spike)
    fc_summary = ", ".join(f"{p:.0f}" for p, _ in data.forecasts[: cfg.forecast_lookahead])
    spot_str = f"{data.spot:.2f}" if data.spot is not None else "?"
    logging.info(
        "Price %.2f c/kWh (spot %s, est=%s, %smin @ %s) | descriptor=%s | spike=%s "
        "| forecast=[%s] | state=%s",
        price, spot_str, data.estimate, data.duration, data.start,
        data.descriptor, data.spike, fc_summary, state["state"])

    # Clear the fast-retry flag; it is set again only if an action fails below.
    state["pending_action"] = False
    # Use the last-known-good address (which may have been auto-discovered).
    host = state.get("airtouch_host") or cfg.airtouch_host
    cidr = cfg.airtouch_discover_cidr or None

    def perform(turn_on: bool, mode: str | None) -> tuple[bool, bool]:
        was_on, now_on, used = do_airtouch(
            host, turn_on=turn_on, mode=mode, dry_run=cfg.dry_run,
            retries=cfg.airtouch_connect_retries, delay=cfg.airtouch_retry_delay_s,
            auto_discover=cfg.airtouch_auto_discover, cidr=cidr)
        if used != host:
            logging.warning("AirTouch address changed: %s -> %s. Remembering it.", host, used)
            state["airtouch_host"] = used
        return was_on, now_on

    if state["state"] == STATE_NORMAL:
        expensive = price >= cfg.upper_threshold_c or spike_now
        if expensive:
            if cooldown_active(state, cfg.cooldown_minutes):
                logging.info("Expensive (%.2f c/kWh, spike=%s) but within cooldown, holding.",
                             price, data.spike)
                return state
            reason = "spike flag" if spike_now and price < cfg.upper_threshold_c else f">= {cfg.upper_threshold_c} c/kWh"
            logging.warning("EXPENSIVE (%s). Curtailing heating.", reason)
            try:
                was_on, now_on = perform(turn_on=False, mode=None)
            except Exception as e:  # noqa: BLE001
                logging.error("Failed to turn AC off: %s. Will retry shortly.", e)
                state["pending_action"] = True  # poll faster until it succeeds
                return state
            state["state"] = STATE_CURTAILED
            state["we_turned_off"] = bool(was_on)  # only restore if we actually turned it off
            state["last_action_ts"] = now_iso()
            if was_on:
                logging.warning("AC turned OFF (was on).")
            else:
                logging.info("AC was already off; will not auto turn it on later.")

    elif state["state"] == STATE_CURTAILED:
        cheap = price <= cfg.lower_threshold_c and not spike_now
        if cheap:
            if cooldown_active(state, cfg.cooldown_minutes):
                logging.info("Cheap again but within cooldown, holding.")
                return state
            # Forecast-aware guard: do not restore into an imminent spike.
            if cfg.forecast_restore_guard:
                risk = forecast_is_risky(cfg, data.forecasts)
                if risk:
                    logging.warning("Cheap now (%.2f c/kWh) but holding heating OFF: %s.",
                                    price, risk)
                    return state  # stay curtailed, do not reset cooldown
            if state.get("we_turned_off"):
                mode_note = f"force {cfg.restore_mode} mode" if cfg.restore_mode else "previous mode preserved"
                logging.warning("CHEAP (%.2f c/kWh <= %.2f). Restoring AC (%s).",
                                price, cfg.lower_threshold_c, mode_note)
                try:
                    perform(turn_on=True, mode=(cfg.restore_mode or None))
                except Exception as e:  # noqa: BLE001
                    logging.error("Failed to turn AC on: %s. Will retry shortly.", e)
                    state["pending_action"] = True
                    return state
                logging.warning("AC turned back ON.")
            else:
                logging.info("Cheap again, but we did not turn the AC off, so leaving it.")
            state["state"] = STATE_NORMAL
            state["we_turned_off"] = False
            state["last_action_ts"] = now_iso()

    return state


def main() -> int:
    load_dotenv()
    cfg = Config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg.log_file),
        ],
    )

    # The airtouch2 library logs a noisy WARNING with a stack trace whenever the
    # unit sends a message type it does not recognise (for example type 0x27).
    # This is benign and recoverable, so quiet it to ERROR by default. Set
    # AIRTOUCH_LOG_LEVEL=WARNING (or DEBUG) in .env to see it again when debugging.
    logging.getLogger("airtouch2").setLevel(
        getattr(logging, os.environ.get("AIRTOUCH_LOG_LEVEL", "ERROR").upper(), logging.ERROR)
    )

    errors = cfg.validate()
    if errors:
        for e in errors:
            logging.error("Config error: %s", e)
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logging.info("Starting monitor. DRY_RUN=%s | upper=%.1f | lower=%.1f | "
                 "trigger_on_spike=%s | poll=%ss | cooldown=%smin | host=%s",
                 cfg.dry_run, cfg.upper_threshold_c, cfg.lower_threshold_c,
                 cfg.trigger_on_spike, cfg.poll_interval_s, cfg.cooldown_minutes,
                 cfg.airtouch_host)
    if cfg.dry_run:
        logging.info("DRY RUN is ON: decisions are logged but the AC will not be switched. "
                     "Set DRY_RUN=false in .env when you are ready.")

    state = load_state(cfg.state_file)
    logging.info("Loaded state: %s", state)

    while _running:
        try:
            state = evaluate_once(cfg, state)
            save_state(cfg.state_file, state)
        except Exception as e:  # noqa: BLE001 - never let the loop die
            logging.error("Cycle error: %s: %s", type(e).__name__, e)

        # If an action is needed but failed (e.g. the AirTouch is briefly
        # unreachable), poll faster so we act the moment it comes back.
        interval = cfg.failed_retry_seconds if state.get("pending_action") else cfg.poll_interval_s

        # Sleep in small steps so signals stop us promptly.
        slept = 0
        while _running and slept < interval:
            time.sleep(min(2, interval - slept))
            slept += 2

    logging.info("Monitor stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

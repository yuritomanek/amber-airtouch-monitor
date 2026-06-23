# amber-airtouch-monitor

Automatically turn your ducted air conditioning off when wholesale electricity
prices spike, and back on when they fall, using live prices from
[Amber Electric](https://www.amber.com.au/) and a Polyaire **AirTouch 2 / 2+**
controller on your local network.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Runs on Raspberry Pi](https://img.shields.io/badge/runs%20on-Raspberry%20Pi%20%2B%20Docker-c51a4a.svg)

## Why

Amber passes through the real-time wholesale price of electricity. Most of the
time that is great and cheap, but a few times a year the price spikes hard. One
night mine jumped past **$25/kWh** (normal is around **$0.40/kWh**) while my
heating ran and I was asleep. The price alert came through overnight and I
missed it.

This is a small always-on service that watches the price for me and acts the
moment it crosses a threshold, so an expensive overnight spike turns the heating
off automatically instead of quietly running up the bill.

## What it does

- Polls your Amber price every couple of minutes (5-minute resolution, the same
  price the Amber app shows and that you are settled on).
- Turns the AirTouch **off** when the price reaches an upper threshold, or when
  Amber itself flags a price **spike**.
- Turns it back **on** when the price falls below a lower threshold.
- **Forecast-aware restore:** before turning the AC back on, it checks the next
  few forecast intervals and stays off if another spike is imminent, so it does
  not flip-flop straight back into an expensive window.
- **Preserves your AC mode:** if your unit runs in AUTO it comes back in AUTO.
  No mode is forced unless you ask for one.
- Edge-triggered with hysteresis and a cooldown, so it acts on threshold
  crossings and will not rapidly cycle the compressor.
- Fails safe: if Amber is unreachable it does nothing and logs a warning.
- Resilient to the AirTouch's IP changing: if its known address stops
  answering, it scans the LAN for the unit, uses it, and remembers the new
  address. It also retries a flaky connection and polls faster while an action
  is pending.
- Persists state to disk, so a restart or reboot does not lose track.

It is also a generic pattern: the "price in, action out" core can drive any
action you like (an HTTP call, a script, another device). Heating is just the
first action.

## How it works

The monitor is a small state machine with two states:

```
        price >= UPPER  (or Amber SPIKE)
   NORMAL ───────────────────────────────▶ CURTAILED
   (heat allowed)                          (we turned heat off)
        ◀───────────────────────────────
        price <= LOWER and no spike
        and forecast is clear
```

Two safety rules keep it predictable:

1. It only turns the AC back **on** if the monitor was the one that turned it
   **off**. If your AC was already off when a spike hit, it will not switch it
   on later.
2. A cooldown enforces a minimum gap between switching actions.

Example log:

```
Price 95.34 c/kWh (spot 65.11, est=False, 5min @ 06:30:01) | descriptor=HIGH | spike=NONE | forecast=[62, 61, 64] | state=normal
WARNING EXPENSIVE (>= 80.0 c/kWh). Curtailing heating.
WARNING AC turned OFF (was on).
...
WARNING CHEAP (50.98 c/kWh <= 60.00). Restoring AC (previous mode preserved).
WARNING AC turned back ON.
```

## Requirements

- An **Amber Electric** account and an API token (free, from
  [app.amber.com.au/developers](https://app.amber.com.au/developers)).
- A **Polyaire AirTouch 2 or 2+** reachable on your local network.
- An always-on machine on the same LAN to run it. A **Raspberry Pi** is ideal.
- Either **Docker** (recommended) or **Python 3.10+**.

## Quick start

```bash
git clone https://github.com/yuritomanek/amber-airtouch-monitor.git
cd amber-airtouch-monitor

cp .env.example .env
# edit .env: paste your Amber token, set AIRTOUCH_HOST and your thresholds.
# Leave DRY_RUN=true for the first run.
```

### Confirm connectivity first

This reads your current price and your AirTouch status without changing
anything:

```bash
# with Docker
docker compose run --rm energy-automation python3 test_connection.py

# or locally
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 test_connection.py
```

### Run the monitor

```bash
# with Docker (recommended: restarts on boot, runs in the background)
mkdir -p data
docker compose up -d --build
docker compose logs -f energy-automation

# or locally
python3 monitor.py
```

While `DRY_RUN=true` it logs every decision ("DRY RUN: would turn OFF the AC")
without touching the unit. Watch it for a day, confirm the decisions look right,
then set `DRY_RUN=false` in `.env` and restart to go live.

See [DEPLOYMENT.md](DEPLOYMENT.md) for running it 24/7 on a Pi and for an
optional Home Assistant control path, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for the full design, build phases, and the reasoning behind the threshold logic.

## Configuration

All configuration lives in `.env` (see `.env.example`).

| Variable | Default | Description |
| --- | --- | --- |
| `AMBER_API_TOKEN` | (required) | Your Amber API token. |
| `AMBER_SITE_ID` | (auto) | Optional. Looked up via the API if blank. |
| `AIRTOUCH_HOST` | (required) | Your AirTouch's LAN IP. |
| `UPPER_THRESHOLD_C` | `100` | Turn heat OFF at/above this many c/kWh. |
| `LOWER_THRESHOLD_C` | `80` | Turn heat back ON at/below this. Must be below upper. |
| `TRIGGER_ON_SPIKE` | `true` | Also curtail when Amber flags a SPIKE. |
| `PRICE_RESOLUTION_MINUTES` | `5` | Price granularity (5 or 30). 5 matches the app. |
| `POLL_INTERVAL_SECONDS` | `120` | How often to check the price. |
| `COOLDOWN_MINUTES` | `15` | Minimum gap between switching actions. |
| `FORECAST_RESTORE_GUARD` | `true` | Hold the restore if a spike is forecast soon. |
| `FORECAST_LOOKAHEAD` | `3` | How many forecast intervals to check. |
| `RESTORE_MODE` | (blank) | Force a mode on restore (HEAT/COOL/AUTO/...). Blank preserves current. |
| `DRY_RUN` | `true` | Log decisions without switching the AC. |
| `AIRTOUCH_CONNECT_RETRIES` | `3` | Retries for a flaky connection within one action. |
| `AIRTOUCH_RETRY_DELAY_SECONDS` | `5` | Delay between those retries. |
| `FAILED_RETRY_SECONDS` | `30` | Poll interval while an action is pending after a failure. |
| `AIRTOUCH_AUTO_DISCOVER` | `true` | Scan the LAN for the AirTouch if its IP stops answering. |
| `AIRTOUCH_DISCOVER_CIDR` | (auto) | Network to scan, e.g. `192.168.0.0/23`. Blank derives a /24 from the host. |

## Safety and disclaimer

This software controls real heating and cooling hardware. Please read this.

- **Not affiliated with Amber Electric or Polyaire / AirTouch.** This is an
  independent, community project. It is not endorsed by or supported by either
  company. "Amber" and "AirTouch" are referenced only to describe what the tool
  talks to.
- It builds on the community libraries [`airtouch2`](https://pypi.org/project/airtouch2/)
  and [`amberelectric`](https://pypi.org/project/amberelectric/). The AirTouch
  library is an unofficial, reverse-engineered client and is marked alpha. Test
  carefully.
- **Use at your own risk.** Provided "as is", with no warranty (see
  [LICENSE](LICENSE)). You are responsible for your hardware and your bills.
- Always run in `DRY_RUN=true` first and confirm the decisions in the log before
  going live. Test the off/on control during the day before trusting it
  overnight.

## Roadmap

- Optional notifications (ntfy / Pushover / Telegram) when it switches.
- Restore the exact previous setpoint as well as the mode.
- A small web UI to manage thresholds and view the action history.
- Multiple rules and multiple devices / generic actions.

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

- [Amber Electric](https://www.amber.com.au/) for offering a genuinely open
  public API that makes this kind of automation possible.
- [`airtouch2`](https://github.com/nathanvdh/airtouch2-python) by Nathan Van der
  Hoek, the local-network client for AirTouch 2 / 2+ systems.
- [`amberelectric`](https://github.com/madpilot/amberelectric.py), the Amber
  Electric Python API client.

## License

[MIT](LICENSE) © 2026 Yuri Tomanek

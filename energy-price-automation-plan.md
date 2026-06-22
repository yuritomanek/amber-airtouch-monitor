# Energy Price Automation: Plan and Architecture

Prepared 22 June 2026. Stack: Raspberry Pi running Docker. Price source: Amber Electric. Heating control: Polyaire AirTouch 2+ (ducted reverse-cycle), controllable over the local network.

## 1. What you are building

A small always-on service that polls your Amber Electric prices, compares them against an upper and a lower threshold, and fires a configurable action when a threshold is crossed. The classic case: when the wholesale price spikes overnight (you saw over $25/kWh against a normal ~$0.40/kWh), automatically turn the heating off, then turn it back on when the price drops below the lower threshold.

Two parts, exactly as you framed it:

1. A **monitor** that watches the Amber price.
2. An **action engine** that lets you define actions (usually an HTTP call to another API, or a short sequence of calls) and bind them to threshold events.

## 2. The price source: Amber Electric API

Amber exposes a documented REST API, which is a good fit.

- **Base URL:** `https://api.amber.com.au/v1`
- **Auth:** Bearer token. You generate it yourself at `app.amber.com.au/developers` (turn on Developer Mode, generate a token). Sent as `Authorization: Bearer <token>`.
- **Get your site id:** `GET /sites` returns your site(s). You need the `siteId` for price calls.
- **Current price plus forecast:** `GET /sites/{siteId}/prices/current?next=N&previous=M`. Returns the current interval plus N future forecast intervals and M past intervals. Prices are in cents per kWh in the `perKwh` field.
- **Channels:** responses are per channel: `general` (your main usage), `controlledLoad`, and `feedIn` (solar export). For heating you care about `general`.
- **Spike signal built in:** each interval carries a `descriptor` (for example `spike`, `high`, `neutral`, `low`, `veryLow`) and a `spikeStatus`. You can trigger off these directly instead of, or in addition to, a raw price number. That is a nice safety net because Amber itself classifies the spike.
- **Rate limit:** roughly 50 calls per 5 minutes per account, and prices only update every 5 minutes. Polling once every 1 to 2 minutes is well within budget and gives you fast reaction.

Design implication: because forecasts are available, you can act early. You can turn heating off when a spike is *forecast* for the next interval rather than waiting until you are already in it.

## 3. The action side: controlling the AirTouch 2+

Your heater is a Polyaire AirTouch 2+, a controller for ducted reverse-cycle air conditioning. The good news is that it is controllable over your **local network** with no cloud dependency, and existing open-source tooling already exists. You have two viable paths:

- **Path A: control the AirTouch directly from the app (simplest).** There is a Python library, `airtouch2` on PyPI, that supports both the AirTouch 2 and 2+ and talks to the unit over TCP on your LAN. If your app is written in Python (recommended in section 5), it can import this library and turn the AC unit off and on itself. No extra services. Fewest moving parts.
- **Path B: control it through Home Assistant (more flexible).** A Home Assistant custom integration, `nathanvdh/homeassistant-airtouch2plus` (installed via HACS), exposes the AirTouch as a standard climate entity. Your app then calls one stable Home Assistant REST endpoint (`climate.set_hvac_mode` with `off` / `heat`). This is worth it if you also want a dashboard, app control of zones, other automations, or you might add more devices later. Home Assistant also has a built-in Amber integration, so you could prototype the entire spike-protection idea inside Home Assistant first, with no custom code, to prove it works.

**Recommendation:** start with Path A for the first working version because it is the least to set up and debug. Keep the action layer abstract (see section 5) so that if you later stand up Home Assistant, switching to Path B is a config change, not a rewrite.

**Important caveat:** both the `airtouch2` library and the AirTouch 2+ HACS integration are community, in-development projects, not official Polyaire software. Pin to a known version and test the off and on commands carefully before trusting them overnight. Confirm whether you want to switch the whole AC unit off (best for spike protection) versus closing individual zones.

## 4. Core logic: how triggers should actually fire

This is the part that is easy to get subtly wrong, so it is worth being explicit.

- **Edge-triggered, not level-triggered.** Fire the action when the price *crosses* a threshold, not on every poll while it stays above. Otherwise you would re-send "turn off heating" every minute.
- **Two thresholds, two actions.** Upper threshold crossed upward fires the "expensive" action (turn heating off). Price falling back below the lower threshold fires the "cheap" action (turn heating back on, or run a load you want to run cheaply). Keep the two thresholds apart so it does not flap around a single value.
- **Hysteresis and cooldown.** Require the price to be clearly back under the lower threshold before re-enabling, and add a minimum time between firings (for example 15 minutes) to avoid rapid on/off cycling that is bad for an aircon compressor.
- **State memory.** Persist the last known state (heating commanded off or on) so a restart of the Pi or container does not lose track or double-fire.
- **Fail-safe behaviour.** Decide explicitly what happens if the Amber API is unreachable. Sensible default: do nothing to the heater and send you an alert, rather than guessing.
- **Manual override.** A way for you to say "leave it alone" (for example if you are awake and want heat regardless of price) that the automation respects.
- **Optional forecast lead time.** Use the forecast intervals to act one interval early on a predicted spike.

## 5. Architecture

A single small service plus optional Home Assistant, all in Docker Compose on the Pi.

```
                         Raspberry Pi (Docker)
  +-------------------------------------------------------------+
  |                                                             |
  |   energy-automation (your app)                              |
  |   +-----------------------------------------------------+   |
  |   |  Scheduler  ->  Price client (Amber)                |   |
  |   |      |              |                               |   |
  |   |      v              v                               |   |
  |   |  Rule engine (thresholds, hysteresis, state)        |   |
  |   |      |                                              |   |
  |   |      v                                              |   |
  |   |  Action executor (HTTP calls / command sequences)   |   |
  |   |      |                  |                           |   |
  |   |      v                  v                           |   |
  |   |  Config + state DB   Notifier (phone push)          |   |
  |   +-----------------------------------------------------+   |
  |          |  small web UI / REST API to manage rules+actions |
  |          v                                                  |
  |   Home Assistant (optional) -> your heater / devices        |
  |                                                             |
  +-------------------------------------------------------------+
                |                          |
                v                          v
          Amber API (cloud)         Your phone (push alert)
```

### Components

1. **Scheduler.** Polls Amber every 1 to 2 minutes.
2. **Price client.** Wraps the Amber API (auth, get site, get current and forecast prices). A Python SDK exists (`amberelectric` on PyPI) if you go Python.
3. **Rule engine.** Holds the threshold rules, applies edge detection, hysteresis, cooldown, and the current state. Decides whether an action should fire.
4. **Action executor.** Runs the action bound to the event. An action is a declarative thing: an HTTP method, URL, headers, and body, or an ordered list of such steps for "a series of commands". Supports simple templating so you can inject values (current price, timestamp) into the request.
5. **Config and state store.** SQLite is plenty on a Pi. Stores rules, action definitions, last state, and an audit log of what fired and when.
6. **Notifier.** Push to your phone so you are informed even though it acted for you. Good options: ntfy (self-hostable, free), Pushover, or Telegram. This directly solves your "I was asleep" problem.
7. **Web UI / REST API.** A small interface to create and edit actions and rules without redeploying. This is your requirement 2 ("ability to create actions"). Can start as a config file and grow into a small web form.

### Suggested tech stack

- **Language:** Python. Amber has a Python SDK, and the logic is light. Node.js is a fine alternative if you prefer it.
- **Framework:** FastAPI (gives you the REST API and a simple UI surface for free).
- **Scheduling:** APScheduler, or just an async loop.
- **Storage:** SQLite.
- **Packaging:** one Docker image, orchestrated with Docker Compose (optionally a second service for Home Assistant).
- **Notifications:** ntfy or Pushover.

### What an action definition looks like (illustrative)

```yaml
actions:
  heating_off:
    steps:
      - method: POST
        url: http://homeassistant:8123/api/services/climate/set_hvac_mode
        headers:
          Authorization: "Bearer {{HA_TOKEN}}"
        body:
          entity_id: climate.lounge
          hvac_mode: "off"
  heating_on:
    steps:
      - method: POST
        url: http://homeassistant:8123/api/services/climate/set_hvac_mode
        headers:
          Authorization: "Bearer {{HA_TOKEN}}"
        body:
          entity_id: climate.lounge
          hvac_mode: "heat"

rules:
  - name: spike_protection
    channel: general
    upper_threshold_c_per_kwh: 100      # turn off above $1.00/kWh
    lower_threshold_c_per_kwh: 60       # turn back on below $0.60/kWh
    on_upper: heating_off
    on_lower: heating_on
    cooldown_minutes: 15
    use_amber_spike_descriptor: true    # also trip on Amber's own "spike" flag
```

## 6. Safety and reliability notes

- The heater action only ever runs through one tested path (ideally Home Assistant), so failures are contained and easy to reason about.
- Keep an audit log of every fire with the price that triggered it, so you can review overnight behaviour in the morning.
- Always notify, even on success. The automation acting silently is what burned you; visibility is the fix.
- Add a "panic" override and a fail-safe default for API outages, as in section 4.
- Be conscious of aircon compressor wear: cooldowns prevent rapid cycling.

## 7. Build phases

1. **Prove the two ends work.** Confirm you can read your Amber price via the API, and that you can turn the AirTouch off and on with the `airtouch2` library over your LAN (find its IP, run a small test script). This de-risks everything before wiring them together.
2. **Minimum viable monitor.** Python service: poll Amber, evaluate one upper and one lower threshold with edge detection and cooldown, call the AirTouch off/on action, push a notification. Config in a YAML file. Runs in Docker.
3. **Action engine generalised.** Move actions and rules into SQLite, support multi-step HTTP sequences and templating, add the fail-safe and override logic.
4. **Management UI.** Small FastAPI web page to create and edit actions and rules, view the audit log, and flip the manual override.
5. **Polish.** Forecast-based early action, multiple rules, multiple devices.

## 8. Open questions to resolve next

1. **Direct or via Home Assistant?** Path A (app controls the AirTouch directly with the `airtouch2` library) or Path B (through Home Assistant). Path A is recommended to start.
2. **AirTouch IP address.** The unit needs a reachable, ideally fixed, local IP. Worth setting a DHCP reservation on your router so it does not change.
3. **Off whole unit or zones?** For spike protection, switching the whole AC unit off is simplest and most effective. Confirm that is the intended behaviour.
4. **Language preference:** Python (recommended, since both the Amber SDK and the `airtouch2` library are Python) or Node?
5. **Notifications:** preference between ntfy, Pushover, or Telegram?
6. **Thresholds:** what upper and lower prices do you want to act on? We can start conservative and tune.

## Sources

- [Do you have an API? - Amber Electric](https://help.amber.com.au/hc/en-us/articles/360038985552-Do-you-have-an-API)
- [Amber public API repo and discussions](https://github.com/amberelectric/public-api)
- [Amber API rate limiting discussion](https://github.com/amberelectric/public-api/discussions/146)
- [amberelectric Python SDK (PyPI)](https://pypi.org/project/amberelectric/)
- [Amber Electric integration - Home Assistant](https://www.home-assistant.io/integrations/amberelectric/)
- [Advanced Price Forecasting - Amber Electric](https://help.amber.com.au/hc/en-us/articles/28605992738445-Advanced-Price-Forecasting)
- [airtouch2 Python library (PyPI)](https://pypi.org/project/airtouch2/)
- [homeassistant-airtouch2plus integration (GitHub)](https://github.com/nathanvdh/homeassistant-airtouch2plus)

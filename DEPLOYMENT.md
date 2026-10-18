# Deployment: Docker and the two control paths

This explains how to run the project on your Raspberry Pi in Docker, and the two
ways to control the AirTouch 2+. Pick one control path now; you can switch later
because the action layer is kept separate from the monitoring logic.

## The two control paths

### Path A: app controls the AirTouch directly (recommended to start)

Your app talks to the AirTouch over your LAN using the `airtouch2` library on TCP
port 9200. Nothing else to install. Fewest moving parts. This is what
`test_connection.py` already does.

```
energy-automation (Docker) --TCP 9200--> AirTouch 2+ (AIRTOUCH_HOST)
        |
        +--HTTPS--> Amber API
        +--push--> your phone (later)
```

Use Path A if you just want the spike protection working with minimum setup.

### Path B: control through Home Assistant

Home Assistant runs on the Pi and owns the AirTouch via a community integration.
Your app then only calls Home Assistant's REST API. Worth it if you also want
dashboards, zone control, phone app, and other automations.

```
energy-automation (Docker) --REST--> Home Assistant (Docker) --LAN--> AirTouch 2+
        |
        +--HTTPS--> Amber API
```

## Running on the Pi (both paths)

Prerequisites: Docker and the Docker Compose plugin installed on the Pi.

### One-time setup

```bash
# from the project folder on the Pi
cp .env.example .env          # then edit .env: paste your Amber token,
                              # set your thresholds, leave DRY_RUN=true for now
mkdir -p data                 # holds the state file and audit log (see below)
```

### Optional: confirm connectivity first

```bash
# one-off connectivity test (builds the image if needed)
docker compose run --rm energy-automation python3 test_connection.py

# test actual AC control (briefly cycles the unit, only if it is on)
docker compose run --rm energy-automation python3 test_connection.py --cycle
```

### Run the monitor

The default command in `docker-compose.yml` is the monitor, so you just bring
the service up. It runs in the background and restarts automatically.

```bash
# build and start the monitor in the background
docker compose up -d --build

# watch what it is doing (Ctrl-C to stop watching; the monitor keeps running)
docker compose logs -f energy-automation
```

While `DRY_RUN=true` you will see lines like "DRY RUN: would turn OFF the AC".
The state file and log are written to the `data/` folder on the Pi (mapped into
the container), so they survive restarts. You can also read the log directly:

```bash
tail -f data/monitor.log
```

### Going live

Once you are happy with the decisions in the log:

```bash
# 1. edit .env and set:
#    DRY_RUN=false
# 2. recreate the container so it picks up the change:
docker compose up -d
```

`docker compose up -d` re-reads `.env` and restarts the service. No rebuild is
needed for a config change because the code is unchanged.

### Everyday commands

```bash
docker compose ps                       # is it running?
docker compose logs --tail 50 energy-automation   # recent activity
docker compose restart energy-automation          # restart it
docker compose down                     # stop and remove the container
```

`restart: unless-stopped` in `docker-compose.yml` means the service comes back
automatically after a reboot or crash, which is what you want for an overnight
guard. After a Pi reboot, Docker starts it again on its own (as long as the
Docker daemon is enabled at boot, which is the default).

## Path B setup: Home Assistant + AirTouch 2+

Only do this if you chose Path B.

1. Uncomment the `homeassistant` service in `docker-compose.yml`, then
   `docker compose up -d homeassistant`.
2. Open `http://<pi-ip>:8123` and complete the initial setup.
3. Install HACS (the Home Assistant Community Store), which is how the AirTouch
   2+ integration is distributed: https://hacs.xyz/
4. In HACS, add the custom repository `nathanvdh/homeassistant-airtouch2plus`
   as an Integration, then install it.
5. Restart Home Assistant, then add the AirTouch integration and point it at
   your AirTouch's IP. Your AC appears as a `climate.*` entity.
6. Create a Long-Lived Access Token in your HA user profile. Your app uses this
   to call the REST API.

Turning the AC off and on from your app then looks like this:

```bash
# turn off
curl -X POST http://<pi-ip>:8123/api/services/climate/set_hvac_mode \
  -H "Authorization: Bearer <HA_LONG_LIVED_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "climate.airtouch_2_plus", "hvac_mode": "off"}'

# turn back on (heat)
curl -X POST http://<pi-ip>:8123/api/services/climate/set_hvac_mode \
  -H "Authorization: Bearer <HA_LONG_LIVED_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "climate.airtouch_2_plus", "hvac_mode": "heat"}'
```

The exact entity id is shown in Home Assistant under Settings > Devices &
Services > Entities after the integration is added.

Tip: you can also prototype the whole spike automation inside Home Assistant
using its built-in Amber Electric integration and an automation, with no custom
code, to validate the idea before building the app further.

## What comes next

The monitor (`monitor.py`) is built and is the `energy-automation` container's
default command. Planned additions, such as notifications when it switches and
restoring the exact setpoint, are listed in the README roadmap. See
`docs/ARCHITECTURE.md` for the reasoning behind the threshold logic.

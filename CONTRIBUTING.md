# Contributing

Thanks for your interest in amber-airtouch-monitor. This is a small community
project and contributions are welcome, whether that is a bug report, a docs
fix, or a feature.

## Ways to help

- Report a bug or unexpected behaviour by opening an issue. Please include the
  relevant lines from your log (with any tokens or personal details removed).
- Suggest a feature or improvement via an issue before opening a large PR, so we
  can agree on the approach.
- Improve the docs. Clearer setup steps help everyone.

## Development setup

```bash
git clone https://github.com/yuritomanek/amber-airtouch-monitor.git
cd amber-airtouch-monitor

python3 -m venv .venv        # Python 3.10+ required (airtouch2 needs it)
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env         # add your Amber token and AirTouch IP
```

Use the connectivity test to confirm your environment talks to both ends:

```bash
python3 test_connection.py
```

## Guidelines

- Keep `DRY_RUN=true` while developing so you do not switch real hardware by
  accident. The monitor logs every decision in dry run.
- The decision logic in `monitor.py` is covered by quick simulations. If you
  change the state machine (thresholds, cooldown, forecast guard, restore
  behaviour), please add or update a simulation so the edge cases stay verified.
- Avoid committing secrets. `.env`, `data/`, state and log files are gitignored.
- This project depends on two community / generated libraries (`airtouch2` and
  `amberelectric`). They are pinned in `requirements.txt`. If you bump them,
  re-run the connectivity test and note it in your PR.

## Code of conduct

Be kind and constructive. We are all here to keep our power bills down.

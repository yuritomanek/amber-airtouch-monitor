# Design notes: the threshold logic

The [README](../README.md) covers what the tool does and how to run it. This
doc covers the part that is easy to get subtly wrong: why the decision logic is
shaped the way it is, and how to choose your numbers. Most of it comes from
watching it run against real Amber price data.

## Two thresholds, not one

Curtail at an upper price, restore at a lower one, with a deliberate gap between
them. That gap is hysteresis, and it exists because the price often sits right at
a single value and wobbles. If you turned off and on at the same number, a price
hovering near it would switch the AC repeatedly. With `UPPER=100` and `LOWER=80`,
the price has to genuinely fall 20c below where it triggered before heat comes
back, so it cannot oscillate around one point.

## Choosing the numbers

Anchor both thresholds to your own normal price. If you normally pay around 40
c/kWh:

- **Upper (curtail):** set it clearly above normal so you only act when power is
  genuinely expensive, not merely a bit high. 80 to 100 c/kWh is a sensible
  starting band. Lower means more aggressive saving but more interruptions.
- **Lower (restore):** this one is more subtle than it looks. Setting it too
  close to the upper threshold makes the monitor restore eagerly, and during a
  volatile spike the price often bounces. Keep it nearer your true "cheap"
  (around or just above normal) so heat only comes back when prices have actually
  settled, not during a lull mid-spike.

### A real example of getting lower wrong

In live testing with `UPPER=80`, `LOWER=60`:

```
04:21  124 c/kWh   -> curtail (turn AC off)
04:31   51 c/kWh   -> restore (turn AC on)      LOWER=60 fired here
04:35  116 c/kWh   -> wants to curtail again, but cooldown is holding
```

The restore at 51c looked cheap, but Amber itself only rated that interval
NEUTRAL, and four minutes later the price was back at 116c. A lower threshold
nearer 40 to 45c would have kept the AC off through that whole choppy stretch
instead of flicking it back on into the next jump. This is exactly what the
forecast guard (below) was added to prevent.

## Cooldown: protection with a cost

A minimum gap between switching actions stops the monitor cycling the compressor
on and off rapidly, which is hard on the hardware. The cost is reaction speed.
In the example above, the AC had just been restored at 04:31, so when the price
jumped to 116c at 04:35 the cooldown (10 min there) kept it on for several more
minutes before it could curtail again.

So the cooldown trades a little exposure during whipsaws for compressor
longevity. Shorten it for faster reaction and more cycling; lengthen it for the
opposite. The forecast guard reduces how often this trade-off bites, because it
avoids the premature restore that starts the problem.

## Price resolution: read the same number the app shows

Amber's API returns prices at your account's billing-interval resolution by
default, often 30 minutes, while the app shows the live 5-minute price and you
are settled on 5-minute intervals. During volatile periods these diverge a lot:
in testing the API's default read 50.98c while the app showed ~80c at the same
moment, partly because the current interval is an *estimate* that gets revised.
The monitor therefore requests 5-minute resolution explicitly, so it acts on the
same price you see and pay.

## Triggering on Amber's spike flag

Each interval carries Amber's own `spikeStatus`. Acting on a SPIKE flag in
addition to the numeric threshold catches genuine spikes that a fixed number
might miss, and lets Amber's own classification back up your threshold. POTENTIAL
spikes are treated as a heads-up (they hold a restore) but do not curtail on
their own.

## Forecast-aware restore

The lesson from the 04:31 example: cheap right now is not enough if expensive is
about to return. Before restoring, the monitor looks at the next few forecast
intervals and stays off if any is at or above the upper threshold or is flagged
SPIKE/POTENTIAL. This directly prevents restoring into an imminent second spike,
and it makes the exact value of the lower threshold less critical.

## Why edge-triggered, and the safety rules

- **Edge-triggered, not level-triggered.** The monitor acts on a state change
  (price crossing a threshold), not on every poll, so it issues one command per
  transition rather than repeating it every cycle.
- **Only restores what it curtailed.** It tracks whether it was the one that
  turned the AC off. If the unit was already off when a spike hit, it will not
  switch it on later. This keeps it from surprising you.
- **Manual override has one edge.** If you manually turn heat back on during a
  curtailment, the monitor leaves it on (it does not fight you), but that also
  means spike protection is effectively paused until the price cycles down and a
  fresh crossing occurs. Worth knowing if you override mid-spike.
- **Fail-safe.** If the price cannot be read, it does nothing that cycle and logs
  a warning rather than acting on stale or missing data.

## Mode preservation

Curtailing sends a power-off only, leaving the AirTouch's mode untouched, so a
unit running in AUTO comes back in AUTO when restored. A specific mode is only
forced if you set `RESTORE_MODE`.

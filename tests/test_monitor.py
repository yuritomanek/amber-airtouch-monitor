"""Unit tests for the monitor's decision logic.

Network calls (Amber API and AirTouch) are mocked, so these run anywhere:

    python3 -m unittest discover -s tests
"""

import logging
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import monitor  # noqa: E402


def setUpModule():
    # Keep test output clean; assertLogs still captures what it needs.
    logging.getLogger().setLevel(logging.CRITICAL)

SPIKE = "SpikeStatus.SPIKE"
POTENTIAL = "SpikeStatus.POTENTIAL"
NONE = "SpikeStatus.NONE"


def make_cfg(**env):
    base = {
        "AMBER_API_TOKEN": "test-token",
        "AIRTOUCH_HOST": "10.0.0.5",
        "UPPER_THRESHOLD_C": "100",
        "LOWER_THRESHOLD_C": "80",
        "COOLDOWN_MINUTES": "15",
        "DRY_RUN": "false",
    }
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=True):
        return monitor.Config()


def make_data(price, spike=NONE, forecasts=None):
    return monitor.AmberData(price=price, descriptor="PriceDescriptor.NEUTRAL",
                             spike=spike, forecasts=forecasts or [])


def normal_state():
    return {"state": monitor.STATE_NORMAL, "we_turned_off": False, "last_action_ts": None}


def curtailed_state(we_turned_off=True, minutes_ago=60):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"state": monitor.STATE_CURTAILED, "we_turned_off": we_turned_off,
            "last_action_ts": ts}


class SpikeHelperTests(unittest.TestCase):
    def test_is_spike(self):
        self.assertTrue(monitor.is_spike(SPIKE))
        self.assertFalse(monitor.is_spike(POTENTIAL))
        self.assertFalse(monitor.is_spike(NONE))

    def test_is_potential_spike(self):
        self.assertTrue(monitor.is_potential_spike(POTENTIAL))
        self.assertFalse(monitor.is_potential_spike(SPIKE))


class ConfigTests(unittest.TestCase):
    def test_lower_must_be_below_upper(self):
        cfg = make_cfg(LOWER_THRESHOLD_C="100")
        self.assertTrue(any("LOWER_THRESHOLD_C" in e for e in cfg.validate()))

    def test_missing_token(self):
        cfg = make_cfg(AMBER_API_TOKEN="")
        self.assertTrue(any("AMBER_API_TOKEN" in e for e in cfg.validate()))

    def test_valid(self):
        self.assertEqual(make_cfg().validate(), [])


class ForecastRiskTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(FORECAST_LOOKAHEAD="2")

    def test_clear_forecast(self):
        self.assertIsNone(monitor.forecast_is_risky(self.cfg, [(50, NONE), (60, NONE)]))

    def test_expensive_forecast(self):
        self.assertIn("+2", monitor.forecast_is_risky(self.cfg, [(50, NONE), (100, NONE)]))

    def test_spike_and_potential_flags(self):
        self.assertIn("SPIKE", monitor.forecast_is_risky(self.cfg, [(50, SPIKE)]))
        self.assertIn("POTENTIAL", monitor.forecast_is_risky(self.cfg, [(50, POTENTIAL)]))

    def test_ignores_intervals_past_lookahead(self):
        fc = [(50, NONE), (50, NONE), (500, SPIKE)]
        self.assertIsNone(monitor.forecast_is_risky(self.cfg, fc))


class CooldownTests(unittest.TestCase):
    def test_no_previous_action(self):
        self.assertFalse(monitor.cooldown_active({"last_action_ts": None}, 15))

    def test_recent_action(self):
        self.assertTrue(monitor.cooldown_active(curtailed_state(minutes_ago=5), 15))

    def test_old_action(self):
        self.assertFalse(monitor.cooldown_active(curtailed_state(minutes_ago=20), 15))

    def test_bad_timestamp(self):
        self.assertFalse(monitor.cooldown_active({"last_action_ts": "garbage"}, 15))


class EvaluateOnceTests(unittest.TestCase):
    """Drive evaluate_once with mocked price data and a mocked AirTouch."""

    def run_cycle(self, cfg, state, data, ac=None, ac_error=None):
        """ac: (was_on, now_on, host_used) returned by do_airtouch."""
        with mock.patch.object(monitor, "get_amber_data", return_value=data), \
             mock.patch.object(monitor, "do_airtouch") as do_ac:
            if ac_error:
                do_ac.side_effect = ac_error
            else:
                do_ac.return_value = ac or (True, False, cfg.airtouch_host)
            new_state = monitor.evaluate_once(cfg, state)
        return new_state, do_ac

    def setUp(self):
        self.cfg = make_cfg()

    # --- fail safe ---
    def test_no_price_takes_no_action(self):
        state = normal_state()
        new, do_ac = self.run_cycle(self.cfg, dict(state), None)
        do_ac.assert_not_called()
        self.assertEqual(new, state)

    # --- NORMAL -> CURTAILED ---
    def test_expensive_curtails_when_ac_on(self):
        new, do_ac = self.run_cycle(self.cfg, normal_state(), make_data(120),
                                    ac=(True, False, "10.0.0.5"))
        self.assertFalse(do_ac.call_args.kwargs["turn_on"])
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)
        self.assertTrue(new["we_turned_off"])
        self.assertIsNotNone(new["last_action_ts"])

    def test_threshold_is_inclusive(self):
        new, _ = self.run_cycle(self.cfg, normal_state(), make_data(100))
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)

    def test_ac_already_off_is_not_claimed(self):
        new, _ = self.run_cycle(self.cfg, normal_state(), make_data(120),
                                ac=(False, False, "10.0.0.5"))
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)
        self.assertFalse(new["we_turned_off"])

    def test_spike_flag_curtails_below_threshold(self):
        new, do_ac = self.run_cycle(self.cfg, normal_state(), make_data(50, spike=SPIKE))
        do_ac.assert_called_once()
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)

    def test_spike_flag_ignored_when_disabled(self):
        cfg = make_cfg(TRIGGER_ON_SPIKE="false")
        new, do_ac = self.run_cycle(cfg, normal_state(), make_data(50, spike=SPIKE))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_NORMAL)

    def test_potential_spike_does_not_curtail(self):
        new, do_ac = self.run_cycle(self.cfg, normal_state(), make_data(50, spike=POTENTIAL))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_NORMAL)

    def test_below_upper_does_nothing(self):
        new, do_ac = self.run_cycle(self.cfg, normal_state(), make_data(99.9))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_NORMAL)

    def test_expensive_within_cooldown_holds(self):
        state = normal_state()
        state["last_action_ts"] = datetime.now(timezone.utc).isoformat()
        new, do_ac = self.run_cycle(self.cfg, state, make_data(120))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_NORMAL)

    def test_curtail_failure_sets_pending_and_stays_normal(self):
        new, _ = self.run_cycle(self.cfg, normal_state(), make_data(120),
                                ac_error=ConnectionError("down"))
        self.assertEqual(new["state"], monitor.STATE_NORMAL)
        self.assertTrue(new["pending_action"])

    def test_pending_flag_cleared_on_next_good_cycle(self):
        state = normal_state()
        state["pending_action"] = True
        new, _ = self.run_cycle(self.cfg, state, make_data(50))
        self.assertFalse(new["pending_action"])

    def test_discovered_host_is_remembered(self):
        new, _ = self.run_cycle(self.cfg, normal_state(), make_data(120),
                                ac=(True, False, "10.0.0.77"))
        self.assertEqual(new["airtouch_host"], "10.0.0.77")

    def test_remembered_host_is_used(self):
        state = normal_state()
        state["airtouch_host"] = "10.0.0.77"
        _, do_ac = self.run_cycle(self.cfg, state, make_data(120),
                                  ac=(True, False, "10.0.0.77"))
        self.assertEqual(do_ac.call_args.args[0], "10.0.0.77")

    # --- CURTAILED -> NORMAL ---
    def test_cheap_restores_when_we_turned_off(self):
        new, do_ac = self.run_cycle(self.cfg, curtailed_state(), make_data(70),
                                    ac=(False, True, "10.0.0.5"))
        self.assertTrue(do_ac.call_args.kwargs["turn_on"])
        self.assertIsNone(do_ac.call_args.kwargs["mode"])
        self.assertEqual(new["state"], monitor.STATE_NORMAL)
        self.assertFalse(new["we_turned_off"])

    def test_restore_mode_is_passed_through(self):
        cfg = make_cfg(RESTORE_MODE="heat")
        _, do_ac = self.run_cycle(cfg, curtailed_state(), make_data(70))
        self.assertEqual(do_ac.call_args.kwargs["mode"], "HEAT")

    def test_cheap_but_not_ours_skips_ac_and_resets(self):
        new, do_ac = self.run_cycle(self.cfg, curtailed_state(we_turned_off=False),
                                    make_data(70))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_NORMAL)

    def test_between_thresholds_stays_curtailed(self):
        new, do_ac = self.run_cycle(self.cfg, curtailed_state(), make_data(90))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)

    def test_cheap_with_spike_flag_stays_curtailed(self):
        new, do_ac = self.run_cycle(self.cfg, curtailed_state(), make_data(70, spike=SPIKE))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)

    def test_cheap_within_cooldown_holds(self):
        new, do_ac = self.run_cycle(self.cfg, curtailed_state(minutes_ago=5), make_data(70))
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)

    def test_forecast_guard_holds_restore(self):
        data = make_data(70, forecasts=[(60, NONE), (150, NONE)])
        state = curtailed_state()
        ts_before = state["last_action_ts"]
        new, do_ac = self.run_cycle(self.cfg, state, data)
        do_ac.assert_not_called()
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)
        self.assertEqual(new["last_action_ts"], ts_before)

    def test_forecast_guard_disabled_restores(self):
        cfg = make_cfg(FORECAST_RESTORE_GUARD="false")
        data = make_data(70, forecasts=[(150, SPIKE)])
        new, do_ac = self.run_cycle(cfg, curtailed_state(), data)
        do_ac.assert_called_once()
        self.assertEqual(new["state"], monitor.STATE_NORMAL)

    def test_restore_failure_sets_pending_and_stays_curtailed(self):
        new, _ = self.run_cycle(self.cfg, curtailed_state(), make_data(70),
                                ac_error=ConnectionError("down"))
        self.assertEqual(new["state"], monitor.STATE_CURTAILED)
        self.assertTrue(new["we_turned_off"])
        self.assertTrue(new["pending_action"])

    # --- full cycle ---
    def test_spike_then_recovery_round_trip(self):
        cfg = make_cfg(COOLDOWN_MINUTES="0")
        state, _ = self.run_cycle(cfg, normal_state(), make_data(124),
                                  ac=(True, False, "10.0.0.5"))
        self.assertEqual(state["state"], monitor.STATE_CURTAILED)
        state, _ = self.run_cycle(cfg, state, make_data(90))
        self.assertEqual(state["state"], monitor.STATE_CURTAILED)
        state, do_ac = self.run_cycle(cfg, state, make_data(45),
                                      ac=(False, True, "10.0.0.5"))
        self.assertTrue(do_ac.call_args.kwargs["turn_on"])
        self.assertEqual(state["state"], monitor.STATE_NORMAL)


class StatePersistenceTests(unittest.TestCase):
    def test_round_trip_and_corrupt_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            self.assertEqual(monitor.load_state(path)["state"], monitor.STATE_NORMAL)
            monitor.save_state(path, curtailed_state())
            self.assertEqual(monitor.load_state(path)["state"], monitor.STATE_CURTAILED)
            with open(path, "w") as f:
                f.write("{not json")
            with self.assertLogs(level="WARNING"):
                self.assertEqual(monitor.load_state(path)["state"], monitor.STATE_NORMAL)


class AmberSiteCacheTests(unittest.TestCase):
    """get_amber_data should look up the site once, then reuse it."""

    def fake_amber(self):
        class ChannelType:
            GENERAL = "general"
            FEEDIN = "feedIn"

        class ApiException(Exception):
            status = reason = None

        interval = types.SimpleNamespace(
            channel_type=ChannelType.GENERAL, type="CurrentInterval", per_kwh=42.0,
            descriptor="neutral", spike_status=NONE, spot_per_kwh=10.0,
            estimate=False, start_time="now", duration=5)

        api = mock.MagicMock()
        api.get_sites.return_value = [types.SimpleNamespace(id="site-123")]
        feedin = types.SimpleNamespace(
            channel_type=ChannelType.FEEDIN, type="CurrentInterval", per_kwh=21.1)
        feedin_fc = types.SimpleNamespace(
            channel_type=ChannelType.FEEDIN, type="ForecastInterval", per_kwh=-3.0)
        api.get_current_prices.return_value = [interval, feedin, feedin_fc]

        pkg = types.ModuleType("amberelectric")
        pkg.Configuration = mock.MagicMock()
        pkg.ApiClient = mock.MagicMock()
        pkg.AmberApi = mock.MagicMock(return_value=api)
        models = types.ModuleType("amberelectric.models")
        channel = types.ModuleType("amberelectric.models.channel")
        channel.ChannelType = ChannelType
        rest = types.ModuleType("amberelectric.rest")
        rest.ApiException = ApiException
        modules = {"amberelectric": pkg, "amberelectric.models": models,
                   "amberelectric.models.channel": channel, "amberelectric.rest": rest}
        return modules, api

    def test_site_id_looked_up_once(self):
        modules, api = self.fake_amber()
        cfg = make_cfg()
        with mock.patch.dict(sys.modules, modules):
            first = monitor.get_amber_data(cfg)
            second = monitor.get_amber_data(cfg)
        self.assertEqual(first.price, 42.0)
        self.assertEqual(second.price, 42.0)
        api.get_sites.assert_called_once()
        self.assertEqual(cfg.amber_site_id, "site-123")
        self.assertEqual(api.get_current_prices.call_args.args[0], "site-123")

    def test_feedin_channel_is_parsed_with_amber_sign(self):
        modules, _ = self.fake_amber()
        with mock.patch.dict(sys.modules, modules):
            data = monitor.get_amber_data(make_cfg())
        self.assertEqual(data.feedin, 21.1)
        self.assertEqual(data.feedin_forecasts, [-3.0])
        self.assertEqual(data.price, 42.0)

    def test_configured_site_id_skips_lookup(self):
        modules, api = self.fake_amber()
        cfg = make_cfg(AMBER_SITE_ID="mine")
        with mock.patch.dict(sys.modules, modules):
            monitor.get_amber_data(cfg)
        api.get_sites.assert_not_called()



class InverterTests(unittest.TestCase):
    """Inverter curtailment. Export cost is Amber's raw feed-in sign (positive =
    exporting costs money). A full stop only pays when export cost > import."""

    def setUp(self):
        self.cfg = make_cfg(INVERTER_ENABLED="true", INVERTER_HOST="10.0.0.9")

    def data(self, import_c=30.0, export_cost=-5.0, fc_cost=None, fc_import=None):
        prices = fc_import if fc_import is not None else [import_c] * len(fc_cost or [])
        d = make_data(import_c, forecasts=[(p, NONE) for p in prices])
        d.feedin = export_cost
        d.feedin_forecasts = fc_cost or []
        return d

    def cycle(self, state, data, cfg=None, error=None):
        with mock.patch.object(monitor, "do_inverter") as inv:
            if error:
                inv.side_effect = error
            else:
                inv.return_value = "Normal"
            new = monitor.evaluate_inverter(cfg or self.cfg, state, data)
        return new, inv

    def stopped(self, minutes_ago=60):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        return {"inverter_state": monitor.STATE_CURTAILED, "inverter_last_action_ts": ts,
                "inverter_last_price_ts": datetime.now(timezone.utc).isoformat()}

    def test_config_requires_host_and_hysteresis(self):
        errors = make_cfg(INVERTER_ENABLED="true").validate()
        self.assertIn("INVERTER_HOST must be set when INVERTER_ENABLED is true.", errors)
        bad = make_cfg(INVERTER_ENABLED="true", INVERTER_HOST="x",
                       INVERTER_RESTORE_MARGIN_C="0", INVERTER_CURTAIL_MARGIN_C="0")
        self.assertTrue(any("hysteresis" in e for e in bad.validate()))

    def test_normal_prices_do_nothing(self):
        new, inv = self.cycle({}, self.data())
        inv.assert_not_called()
        self.assertNotEqual(new.get("inverter_state"), monitor.STATE_CURTAILED)

    def test_small_export_cost_while_import_is_dearer_does_not_stop(self):
        # Live midday data: import 11.3c, export cost 1.4c. A stop would lose
        # free self-consumption, so do nothing.
        new, inv = self.cycle({}, self.data(import_c=11.3, export_cost=1.4))
        inv.assert_not_called()

    def test_export_costs_more_than_import_stops(self):
        new, inv = self.cycle({}, self.data(import_c=5.0, export_cost=33.0))
        inv.assert_called_once()
        self.assertEqual(new["inverter_state"], monitor.STATE_CURTAILED)

    def test_yesterdays_real_1500_interval_stops(self):
        new, _ = self.cycle({}, self.data(import_c=-11.9, export_cost=21.1))
        self.assertEqual(new["inverter_state"], monitor.STATE_CURTAILED)

    def test_negative_import_alone_stops(self):
        new, inv = self.cycle({}, self.data(import_c=-1.0, export_cost=-40.0))
        inv.assert_called_once()
        self.assertEqual(new["inverter_state"], monitor.STATE_CURTAILED)

    def test_stop_respects_cooldown(self):
        recent = {"inverter_last_action_ts": datetime.now(timezone.utc).isoformat()}
        _, inv = self.cycle(recent, self.data(import_c=5.0, export_cost=33.0))
        inv.assert_not_called()

    def test_stop_failure_sets_pending_and_stays_running(self):
        new, _ = self.cycle({}, self.data(import_c=5.0, export_cost=33.0),
                            error=RuntimeError("udp"))
        self.assertTrue(new["inverter_pending"])
        self.assertNotEqual(new.get("inverter_state"), monitor.STATE_CURTAILED)

    def test_restart_when_import_is_dearer_than_export_cost(self):
        new, inv = self.cycle(self.stopped(), self.data(import_c=11.3, export_cost=-0.02))
        inv.assert_called_once()
        self.assertEqual(new["inverter_state"], monitor.STATE_NORMAL)

    def test_hysteresis_holds_inside_the_gap(self):
        # Spread 1c: export is no longer worse than import, but under the 3c margin.
        _, inv = self.cycle(self.stopped(), self.data(import_c=11.0, export_cost=10.0))
        inv.assert_not_called()

    def test_restart_held_when_forecast_spread_turns_bad(self):
        d = self.data(import_c=30.0, export_cost=-5.0, fc_cost=[-5.0, 40.0], fc_import=[30.0, 10.0])
        _, inv = self.cycle(self.stopped(), d)
        inv.assert_not_called()

    def test_restart_held_when_import_forecast_goes_negative(self):
        d = self.data(import_c=30.0, export_cost=-5.0, fc_cost=[-5.0, -5.0], fc_import=[20.0, -1.0])
        _, inv = self.cycle(self.stopped(), d)
        inv.assert_not_called()

    def test_missing_feedin_does_not_leave_inverter_stuck(self):
        d = self.data(import_c=30.0)
        d.feedin = None
        new, inv = self.cycle(self.stopped(), d)
        inv.assert_called_once()

    def test_blind_failsafe_restarts(self):
        state = self.stopped()
        state["inverter_last_price_ts"] = (
            datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        new, inv = self.cycle(state, None)
        inv.assert_called_once()
        self.assertEqual(new["inverter_state"], monitor.STATE_NORMAL)

    def test_short_outage_keeps_inverter_stopped(self):
        state = self.stopped()
        state["inverter_last_price_ts"] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        _, inv = self.cycle(state, None)
        inv.assert_not_called()

    def test_disabled_inverter_is_never_touched(self):
        cfg = make_cfg()
        with mock.patch.object(monitor, "get_amber_data",
                               return_value=self.data(import_c=5.0, export_cost=33.0)), \
             mock.patch.object(monitor, "do_airtouch", return_value=(True, True, cfg.airtouch_host)), \
             mock.patch.object(monitor, "do_inverter") as inv:
            monitor.evaluate_once(cfg, normal_state())
        inv.assert_not_called()


if __name__ == "__main__":
    unittest.main()

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

        class ApiException(Exception):
            status = reason = None

        interval = types.SimpleNamespace(
            channel_type=ChannelType.GENERAL, type="CurrentInterval", per_kwh=42.0,
            descriptor="neutral", spike_status=NONE, spot_per_kwh=10.0,
            estimate=False, start_time="now", duration=5)

        api = mock.MagicMock()
        api.get_sites.return_value = [types.SimpleNamespace(id="site-123")]
        api.get_current_prices.return_value = [interval]

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

    def test_configured_site_id_skips_lookup(self):
        modules, api = self.fake_amber()
        cfg = make_cfg(AMBER_SITE_ID="mine")
        with mock.patch.dict(sys.modules, modules):
            monitor.get_amber_data(cfg)
        api.get_sites.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.project_grid import ConfirmedFillRecorder, evaluate_grid


class FakeTrading:
    def __init__(self):
        self.calls = []

    def backfill_trade(self, payload):
        self.calls.append(payload)
        return {
            "accepted": True,
            "symbol": payload["symbol"],
            "side": payload["side"],
            "quantity": int(payload["quantity"]),
            "price": float(payload["price"]),
            "cash_after": 10000.0,
            "position_after": 12000,
        }

    def get_account(self, _):
        return {"cash": 10000.0, "initial_cash": 40000.0, "commission_rate": 0.0003}

    def list_positions(self, _):
        return [{"symbol": "588000.SH", "quantity": 12000, "avg_cost": 1.68}]


class FakeStore:
    def __init__(self):
        self.events = []

    def list_events(self, _):
        return self.events


class ProjectGridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "audit.sqlite3"
        self.trading = FakeTrading()
        self.store = FakeStore()
        self.handler = SimpleNamespace(trading=self.trading, store=self.store)

    def fill(self, **updates):
        value = {
            "user_confirmed": True,
            "side": "BUY",
            "quantity": 1000,
            "price": 1.65,
            "trade_date": "2026-09-08",
            "trade_time": "10:00:01",
            "source": "user-confirmed screenshot",
            "idempotency_key": "588000-20260908-100001-buy-1000-16500",
        }
        value.update(updates)
        return value

    def test_confirmed_fill_is_fixed_target_and_idempotent(self):
        recorder = ConfirmedFillRecorder(self.db, self.handler)
        first = recorder.record(self.fill())
        second = recorder.record(self.fill())
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.trading.calls), 1)
        self.assertEqual(self.trading.calls[0]["account_id"], "acct_588000_grid")
        self.assertEqual(self.trading.calls[0]["symbol"], "588000.SH")

    def test_confirmation_and_key_reuse_are_rejected(self):
        recorder = ConfirmedFillRecorder(self.db, self.handler)
        with self.assertRaisesRegex(ValueError, "user_confirmed"):
            recorder.record(self.fill(user_confirmed=False))
        recorder.record(self.fill())
        with self.assertRaisesRegex(ValueError, "different fill data"):
            recorder.record(self.fill(quantity=2000))

    def test_obvious_existing_fill_is_not_duplicated(self):
        self.store.events = [{
            "timestamp": "2026-09-08T10:00:01+08:00",
            "quantity": 1000,
            "price": 1.65,
            "metadata": {"side": "BUY"},
        }]
        recorder = ConfirmedFillRecorder(self.db, self.handler)
        with self.assertRaisesRegex(ValueError, "identical fill"):
            recorder.record(self.fill())
        self.assertEqual(self.trading.calls, [])

    def test_grid_requires_config_without_inventing_values(self):
        memory = SimpleNamespace(current=lambda: [])
        result = evaluate_grid(
            self.handler,
            memory,
            quote_getter=lambda _: {"price": 1.7, "source": "fixture", "retrievedAt": "now"},
            kline_getter=lambda *args, **kwargs: {"bars": []},
        )
        self.assertEqual(result["status"], "needs_grid_config")
        self.assertEqual(set(result["missing_config_fields"]), {
            "lower_price", "upper_price", "reference_price", "spacing_pct",
            "order_quantity", "min_position", "max_position",
        })

    def test_grid_evaluation_respects_position_guards(self):
        memory = SimpleNamespace(current=lambda: [])
        config = {
            "lower_price": 1.5,
            "upper_price": 1.9,
            "reference_price": 1.7,
            "spacing_pct": 2,
            "order_quantity": 1000,
            "min_position": 12000,
            "max_position": 12000,
        }
        bars = [{"close": 1.7, "low": 1.6, "high": 1.8}] * 20
        result = evaluate_grid(
            self.handler,
            memory,
            config,
            quote_getter=lambda _: {"price": 1.665, "source": "fixture", "retrievedAt": "now"},
            kline_getter=lambda *args, **kwargs: {"bars": bars},
        )
        self.assertEqual(result["status"], "evaluated")
        self.assertIn("max_position_reached_no_more_buys", result["signals"])
        self.assertIn("minimum_base_blocks_full_sell", result["signals"])
        self.assertIn("buy_level_reached_but_guardrail_blocks", result["signals"])
        self.assertFalse(result["computed"]["can_buy_one_grid"])
        self.assertFalse(result["computed"]["can_sell_one_grid"])


if __name__ == "__main__":
    unittest.main()

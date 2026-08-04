import csv
import os
import tempfile
import unittest
from fractions import Fraction

from ledger_pnl import LedgerPnlError, calculate_sale, reconcile_ledger


HEADER = [
    "timestamp_pt", "order_id", "symbol", "side", "quantity", "price",
    "notional", "reason", "realized_pnl", "rules_version",
]


class LedgerPnlTests(unittest.TestCase):
    def ledger(self, rows):
        temporary = tempfile.TemporaryDirectory()
        path = os.path.join(temporary.name, "trade-ledger.csv")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADER)
            writer.writeheader()
            writer.writerows(rows)
        self.addCleanup(temporary.cleanup)
        return path

    @staticmethod
    def row(timestamp, order_id, symbol, side, quantity, price, pnl=""):
        return {
            "timestamp_pt": timestamp,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "notional": "1.00",
            "reason": "dip-buy" if side == "buy" else "profit-take",
            "realized_pnl": pnl,
            "rules_version": "abcdef0",
        }

    def test_today_agrees_with_broker_from_precise_ledger_fills(self):
        path = self.ledger([
            self.row("2026-08-03T12:44:24.474-07:00", "buy-pusa", "PUSA", "buy",
                     "108.398116", "2.739900"),
            self.row("2026-08-04T06:37:17.516-07:00", "sell-pusa", "PUSA", "sell",
                     "108.398116", "2.830100", "9.777510063200"),
            self.row("2026-08-04T08:45:40.188-07:00", "buy-thry", "THRY", "buy",
                     "113.878854", "2.629900"),
            self.row("2026-08-04T10:37:05.098-07:00", "sell-thry", "THRY", "sell",
                     "113.878854", "2.731000", "11.501764254000"),
        ])

        document = reconcile_ledger(path)
        sells = [row for row in document["rows"] if row["side"] == "sell"]

        self.assertEqual(document["rounding_policy"],
                         "per-fill-half-away-from-zero-to-cent")
        self.assertEqual(sells[0]["realized_pnl"], "9.7775100632")
        self.assertEqual(sells[1]["realized_pnl"], "11.5131521394")
        self.assertEqual(sells[1]["matched_basis_price"], "2.6299")
        self.assertEqual(sells[1]["recorded_difference"], "0.0113878854")
        self.assertEqual([row["realized_pnl_cents"] for row in sells], [978, 1151])
        self.assertTrue(all(row["pnl_source"] == "matched-ledger-pool" for row in sells))
        total = sum(Fraction(row["realized_pnl"]) for row in sells)
        self.assertEqual(total, Fraction("21.2906622026"))
        self.assertEqual(sum(row["realized_pnl_cents"] for row in sells), 2129)

    def test_multiple_buys_and_partial_sales_preserve_weighted_ledger_pool(self):
        path = self.ledger([
            self.row("2026-08-04T06:00:00-07:00", "buy-1", "TEST", "buy", "1", "10"),
            self.row("2026-08-04T06:01:00-07:00", "buy-2", "TEST", "buy", "3", "12"),
            self.row("2026-08-04T06:02:00-07:00", "sell-1", "TEST", "sell", "2", "13", "3"),
        ])
        sell = reconcile_ledger(path)["rows"][-1]
        self.assertEqual(sell["matched_basis_price"], "11.5")
        self.assertEqual(sell["realized_pnl"], "3")
        prospective = calculate_sale(
            path, "TEST", "2", "14", "2026-08-04T06:03:00-07:00"
        )
        self.assertEqual(prospective["status"], "matched-ledger-pool")
        self.assertEqual(prospective["basis_price"], "11.5")
        self.assertEqual(prospective["realized_pnl"], "5")
        self.assertEqual(prospective["realized_pnl_cents"], 500)

    def test_unmatched_sell_is_visibly_an_estimate_not_false_exactness(self):
        path = self.ledger([
            self.row("2026-08-04T06:00:00-07:00", "sell-only", "TEST", "sell",
                     "2", "13", "4.25"),
        ])
        sell = reconcile_ledger(path)["rows"][0]
        self.assertEqual(sell["pnl_source"], "recorded-estimate")
        self.assertEqual(sell["realized_pnl"], "4.25")
        self.assertEqual(sell["realized_pnl_cents"], 425)
        self.assertIsNone(sell["matched_basis_price"])
        prospective = calculate_sale(
            path, "TEST", "1", "14", "2026-08-04T06:01:00-07:00"
        )
        self.assertEqual(prospective["status"], "unavailable")

    def test_rounds_positive_and_negative_half_cent_away_from_zero_once(self):
        path = self.ledger([
            self.row("2026-08-04T06:00:00-07:00", "buy-pos", "POS", "buy", "1", "1"),
            self.row("2026-08-04T06:01:00-07:00", "sell-pos", "POS", "sell", "1", "1.005"),
            self.row("2026-08-04T06:02:00-07:00", "buy-neg", "NEG", "buy", "1", "1"),
            self.row("2026-08-04T06:03:00-07:00", "sell-neg", "NEG", "sell", "1", "0.995"),
        ])
        sells = [row for row in reconcile_ledger(path)["rows"] if row["side"] == "sell"]
        self.assertEqual([row["realized_pnl_cents"] for row in sells], [1, -1])

    def test_rows_are_reconciled_by_execution_time_not_append_order(self):
        path = self.ledger([
            self.row("2026-08-04T10:00:00-07:00", "later-buy", "TEST", "buy", "1", "10"),
            self.row("2026-08-04T09:00:00-07:00", "earlier-sell", "TEST", "sell",
                     "1", "11", "1"),
        ])
        rows = reconcile_ledger(path)["rows"]
        self.assertEqual([row["side"] for row in rows], ["sell", "buy"])
        self.assertEqual(rows[0]["pnl_source"], "recorded-estimate")
        self.assertIsNone(rows[0]["matched_basis_price"])

    def test_prospective_sale_does_not_consume_a_future_buy(self):
        path = self.ledger([
            self.row("2026-08-04T10:00:00-07:00", "future-buy", "TEST", "buy", "1", "10"),
        ])
        result = calculate_sale(
            path, "TEST", "1", "11", "2026-08-04T09:00:00-07:00"
        )
        self.assertEqual(result["status"], "unavailable")

    def test_eastern_accounting_day_is_derived_from_execution_instant(self):
        path = self.ledger([
            self.row("2026-08-04T21:59:00-07:00", "buy", "TEST", "buy", "1", "10"),
            self.row("2026-08-04T22:30:00-07:00", "sell", "TEST", "sell", "1", "11"),
        ])
        rows = reconcile_ledger(path)["rows"]
        self.assertEqual(rows[0]["day"], "2026-08-04")
        self.assertEqual(rows[0]["day_et"], "2026-08-05")
        self.assertEqual(rows[1]["day_et"], "2026-08-05")

    def test_wrong_pacific_offset_fails_closed(self):
        path = self.ledger([
            self.row("2026-08-04T06:00:00-08:00", "bad-offset", "TEST", "buy", "1", "10"),
        ])
        with self.assertRaisesRegex(LedgerPnlError, "not Pacific time"):
            reconcile_ledger(path)

    def test_duplicate_order_ids_fail_closed(self):
        row = self.row("2026-08-04T06:00:00-07:00", "duplicate", "TEST", "buy", "1", "10")
        path = self.ledger([row, {**row, "timestamp_pt": "2026-08-04T06:01:00-07:00"}])
        with self.assertRaisesRegex(LedgerPnlError, "duplicate order id"):
            reconcile_ledger(path)


if __name__ == "__main__":
    unittest.main()
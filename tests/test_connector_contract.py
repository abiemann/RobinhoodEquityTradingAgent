import io
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import connector_contract


def portfolio_payload(**overrides):
    data = {
        "total_value": "1508.9700",
        "cash": "1508.97",
        "equity_value": "0.0000",
        "buying_power": {"buying_power": "1508.9700"},
    }
    data.update(overrides)
    return {"data": data}


def review_payload(**overrides):
    data = {
        "symbol": "APYX",
        "side": "buy",
        "type": "market",
        "dollar_amount": "301.79",
        "order_checks": {},
        "quote_data": {
            "symbol": "APYX",
            "last_trade_price": "2.96",
            "bid_price": "2.96",
            "ask_price": "2.97",
        },
        "market_data_disclosure": (
            "Bid $2.96 × 300 Q · Ask $2.97 × 400 Q · "
            "Last $2.96 × 100 P. Updated 12:33 PM ET."
        ),
    }
    data.update(overrides)
    return {"data": data}


def saved_scan(**overrides):
    scan = {
        "scan_id": "scan-id",
        "title": "Volume field probe",
        "columns": [
            {"display_name": "Symbol", "visible": True},
            {"display_name": "Last", "visible": True},
            {"display_name": "Relative volume", "visible": True},
            {"display_name": "% Change", "visible": True},
            {"display_name": "Volume", "visible": True},
        ],
        "sorting": "Relative volume desc",
        "cortex_managed": False,
    }
    scan.update(overrides)
    return scan


class PortfolioContractTests(unittest.TestCase):
    def test_accepts_and_preserves_exact_decimal_strings(self):
        receipt = connector_contract.normalize_portfolio(portfolio_payload())
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "action": "portfolio",
                "ok": True,
                "values": {
                    "total_value": "1508.9700",
                    "cash": "1508.97",
                    "equity_value": "0",
                    "buying_power": "1508.9700",
                },
            },
        )

    def test_accepts_legacy_scalar_buying_power_and_exact_json_numbers(self):
        receipt = connector_contract.normalize_portfolio(
            portfolio_payload(
                total_value=Decimal("12.50"),
                cash=12,
                equity_value=Decimal("0E-8"),
                buying_power="2.5E+1",
            )
        )
        self.assertEqual(
            receipt["values"],
            {
                "total_value": "12.50",
                "cash": "12",
                "equity_value": "0",
                "buying_power": "25",
            },
        )

    def test_rejects_negative_nonfinite_binary_float_and_missing_values(self):
        invalid_values = (
            "-0.01",
            "NaN",
            "Infinity",
            1.5,
            True,
            None,
            "",
            " ",
            " 1",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(connector_contract.ContractError):
                    connector_contract.normalize_portfolio(
                        portfolio_payload(cash=value)
                    )
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.normalize_portfolio(
                {"data": {"total_value": "1"}}
            )


class QuoteContractTests(unittest.TestCase):
    def payload(self, **overrides):
        quote = {
            "symbol": "SPY",
            "last_trade_price": "765.180000",
            "previous_close": "765.910000",
        }
        quote.update(overrides)
        return {"data": {"results": [{"quote": quote, "close": None}]}}

    def test_compares_exact_prices_and_formats_the_gate_change(self):
        receipt = connector_contract.inspect_quote(self.payload(), "SPY")
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "action": "quote",
                "ok": True,
                "symbol": "SPY",
                "current_price": "765.180000",
                "previous_close": "765.910000",
                "change_percent": "-0.09531145957096786828739669152",
                "change_percent_display": "-0.10",
                "below_previous_close": True,
            },
        )

    def test_accepts_zero_change_without_negative_zero_display(self):
        receipt = connector_contract.inspect_quote(
            self.payload(last_trade_price="12.50", previous_close="12.50"),
            "SPY",
        )
        self.assertEqual(receipt["change_percent"], "0")
        self.assertEqual(receipt["change_percent_display"], "0.00")
        self.assertFalse(receipt["below_previous_close"])

    def test_rejects_wrong_cardinality_symbol_and_nonexact_prices(self):
        invalid_payloads = (
            {"data": {"results": []}},
            {"data": {"results": [None]}},
            self.payload(symbol="QQQ"),
            self.payload(last_trade_price=765.18),
            self.payload(previous_close="0"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(connector_contract.ContractError):
                    connector_contract.inspect_quote(payload, "SPY")
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_quote(self.payload(), "spy")


class ReviewContractTests(unittest.TestCase):
    def inspect(self, payload=None, **arguments):
        request = {
            "symbol": "APYX",
            "side": "buy",
            "order_type": "market",
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "dollar_amount": "301.79",
        }
        request.update(arguments)
        return connector_contract.inspect_review(
            review_payload() if payload is None else payload,
            **request,
        )

    def test_clean_review_binds_exact_order_and_disclosure(self):
        receipt = self.inspect()
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "action": "review",
                "ok": True,
                "symbol": "APYX",
                "side": "buy",
                "order_type": "market",
                "market_hours": "regular_hours",
                "time_in_force": "gfd",
                "quantity": None,
                "dollar_amount": "301.79",
                "limit_price": None,
                "stop_price": None,
                "clean": True,
                "alert_type": None,
                "order_checks": {},
                "market_data_disclosure": (
                    "Bid $2.96 × 300 Q · Ask $2.97 × 400 Q · "
                    "Last $2.96 × 100 P. Updated 12:33 PM ET."
                ),
            },
        )

    def test_nonempty_checks_are_never_clean_and_preserve_known_alert(self):
        checks = {
            "alert_type": "EQUITY_OVERNIGHT_MARKET_BUY_FTUX_POPUP",
            "equity_overnight_market_buy_ftux_popup_alert_details": {},
        }
        receipt = self.inspect(
            review_payload(order_checks=checks)
        )
        self.assertFalse(receipt["clean"])
        self.assertEqual(
            receipt["alert_type"],
            "EQUITY_OVERNIGHT_MARKET_BUY_FTUX_POPUP",
        )
        self.assertEqual(receipt["order_checks"], checks)

        fractional = self.inspect(
            review_payload(
                order_checks={
                    "alert_type": "EQUITY_FRACTIONALLY_UNTRADABLE_ERROR_BUY"
                }
            )
        )
        self.assertFalse(fractional["clean"])
        self.assertEqual(
            fractional["alert_type"],
            "EQUITY_FRACTIONALLY_UNTRADABLE_ERROR_BUY",
        )

        unknown = {"newBrokerCheck": {"reason": "future schema"}}
        receipt = self.inspect(review_payload(order_checks=unknown))
        self.assertFalse(receipt["clean"])
        self.assertIsNone(receipt["alert_type"])
        self.assertEqual(receipt["order_checks"], unknown)

        legacy = self.inspect(
            review_payload(
                order_checks={"alertType": "LEGACY_CONNECTOR_ALERT"}
            )
        )
        self.assertEqual(legacy["alert_type"], "LEGACY_CONNECTOR_ALERT")

        matching_aliases = self.inspect(
            review_payload(
                order_checks={
                    "alert_type": "SAME_ALERT",
                    "alertType": "SAME_ALERT",
                }
            )
        )
        self.assertEqual(matching_aliases["alert_type"], "SAME_ALERT")

        with self.assertRaises(connector_contract.ContractError):
            self.inspect(
                review_payload(
                    order_checks={
                        "alert_type": "CURRENT_ALERT",
                        "alertType": "DIFFERENT_LEGACY_ALERT",
                    }
                )
            )

    def test_binds_quantity_limit_and_stop_reviews(self):
        limit = connector_contract.inspect_review(
            review_payload(
                side="sell",
                type="limit",
                dollar_amount=None,
                quantity="3.00",
                limit_price="3.10",
            ),
            "APYX",
            "sell",
            "limit",
            "regular_hours",
            "gfd",
            quantity="3",
            limit_price="3.100",
        )
        self.assertEqual(limit["quantity"], "3.00")
        self.assertEqual(limit["limit_price"], "3.10")
        self.assertTrue(limit["clean"])

        stop = connector_contract.inspect_review(
            review_payload(
                side="sell",
                type="stop_market",
                dollar_amount=None,
                quantity="3",
                stop_price="2.50",
            ),
            "APYX",
            "sell",
            "stop_market",
            "regular_hours",
            "gtc",
            quantity="3.0",
            stop_price="2.5",
        )
        self.assertEqual(stop["stop_price"], "2.50")
        self.assertTrue(stop["clean"])

    def test_rejects_missing_or_nonobject_order_checks(self):
        for checks in (None, [], "", "clean"):
            with self.subTest(checks=checks):
                with self.assertRaises(connector_contract.ContractError):
                    self.inspect(review_payload(order_checks=checks))
        missing = review_payload()
        del missing["data"]["order_checks"]
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(missing)

    def test_rejects_response_identity_amount_price_and_metadata_mismatches(self):
        invalid_payloads = (
            review_payload(symbol="OTHER"),
            review_payload(side="sell"),
            review_payload(type="limit"),
            review_payload(dollar_amount="301.78"),
            review_payload(quantity="1"),
            review_payload(quote_data={"symbol": "OTHER"}),
            review_payload(quote_data={}),
            review_payload(market_data_disclosure=[]),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(connector_contract.ContractError):
                    self.inspect(payload)

        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_review(
                review_payload(
                    side="sell",
                    type="limit",
                    dollar_amount=None,
                    quantity="3",
                    limit_price="3.11",
                ),
                "APYX",
                "sell",
                "limit",
                "regular_hours",
                "gfd",
                quantity="3",
                limit_price="3.10",
            )

    def test_requires_nullable_quote_data_and_preserves_optional_disclosure(self):
        missing_quote = review_payload()
        del missing_quote["data"]["quote_data"]
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(missing_quote)

        null_quote = self.inspect(review_payload(quote_data=None))
        self.assertTrue(null_quote["clean"])

        empty_disclosure = self.inspect(
            review_payload(market_data_disclosure="")
        )
        self.assertEqual(empty_disclosure["market_data_disclosure"], "")

        absent_disclosure_payload = review_payload()
        del absent_disclosure_payload["data"]["market_data_disclosure"]
        absent_disclosure = self.inspect(absent_disclosure_payload)
        self.assertIsNone(absent_disclosure["market_data_disclosure"])

    def test_rejects_ambiguous_or_incoherent_request_shapes(self):
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_review(
                review_payload(),
                "APYX",
                "buy",
                "market",
                "regular_hours",
                "gfd",
            )
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_review(
                review_payload(),
                "APYX",
                "buy",
                "market",
                "regular_hours",
                "gfd",
                quantity="1",
                dollar_amount="301.79",
            )
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(market_hours="extended_hours")
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_review(
                review_payload(
                    side="sell",
                    type="stop_market",
                    dollar_amount=None,
                    quantity="3",
                    stop_price="2.50",
                ),
                "APYX",
                "sell",
                "stop_market",
                "regular_hours",
                "gfd",
                quantity="3",
                stop_price="2.50",
            )
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_review(
                review_payload(),
                "APYX",
                "sell",
                "market",
                "regular_hours",
                "gfd",
                dollar_amount="301.79",
            )


class PageContractTests(unittest.TestCase):
    def inspect(
        self,
        kind,
        data,
        purpose="daily-loss-a-discovery-page-0",
        request_cursors=("FIRST",),
    ):
        return connector_contract.inspect_page(
            {"data": data}, kind, purpose, request_cursors
        )

    def test_missing_or_empty_next_is_a_valid_terminal_page(self):
        for kind, rows_key in (("positions", "positions"), ("orders", "orders")):
            for next_shape in ({}, {"next": None}, {"next": ""}):
                with self.subTest(kind=kind, next_shape=next_shape):
                    receipt = self.inspect(
                        kind, {rows_key: [], **next_shape}
                    )
                    self.assertEqual(receipt["action"], "page")
                    self.assertEqual(receipt["kind"], kind)
                    self.assertEqual(receipt["row_count"], 0)
                    self.assertIsNone(receipt["next_cursor"])
                    self.assertTrue(receipt["complete"])

    def test_cursor_url_is_normalized_to_the_exact_cursor_token(self):
        receipt = self.inspect(
            "positions",
            {
                "positions": [],
                "next": "https://api.robinhood.com/positions/?cursor=next-token",
            },
            "daily-loss-a-discovery-positions-0",
        )
        self.assertEqual(receipt["next_cursor"], "next-token")
        self.assertFalse(receipt["complete"])
        self.assertEqual(
            receipt["source_purpose"],
            "daily-loss-a-discovery-positions-0",
        )
        self.assertEqual(receipt["request_cursor"], "FIRST")
        self.assertEqual(receipt["request_cursors"], ["FIRST"])

    def test_page_rejects_noncanonical_first_purpose_before_use(self):
        for purpose in (
            "first-positions-00",
            "first-positions-00-retry",
            "first-positions-0-retry2",
            "first-positions-0-retry-retry",
        ):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(
                connector_contract.ContractError, "expected canonical"
            ):
                self.inspect(
                    "positions", {"positions": []}, purpose=purpose
                )

    def test_page_binds_canonical_first_index_to_cursor_position(self):
        with self.assertRaisesRegex(
            connector_contract.RequestBindingError,
            "page index must equal",
        ):
            self.inspect(
                "positions",
                {"positions": []},
                purpose="first-positions-1",
                request_cursors=("FIRST",),
            )
        receipt = self.inspect(
            "positions",
            {"positions": []},
            purpose="first-positions-1-retry",
            request_cursors=("FIRST", "cursor-two"),
        )
        self.assertEqual(receipt["source_purpose"], "first-positions-1-retry")

    def test_first_positions_namespace_rejects_orders_kind(self):
        with self.assertRaisesRegex(
            connector_contract.RequestBindingError,
            "requires --kind positions",
        ):
            self.inspect(
                "orders", {"orders": []}, purpose="first-positions-0"
            )

    def test_page_rejects_self_repeated_or_overlong_cursor_chain(self):
        with self.assertRaisesRegex(
            connector_contract.ContractError, "already requested cursor"
        ):
            self.inspect(
                "positions",
                {
                    "positions": [],
                    "next": "https://api.robinhood.com/positions/?cursor=repeat",
                },
                request_cursors=("FIRST", "repeat"),
            )
        with self.assertRaisesRegex(
            connector_contract.ContractError, "repeated request cursor"
        ):
            self.inspect(
                "positions",
                {"positions": []},
                request_cursors=("FIRST", "repeat", "repeat"),
            )
        with self.assertRaisesRegex(
            connector_contract.ContractError, "page count exceeds"
        ):
            self.inspect(
                "positions",
                {"positions": []},
                request_cursors=("FIRST",) + tuple(
                    f"cursor-{index}"
                    for index in range(connector_contract.MAX_PAGE_COUNT)
                ),
            )
        maximum_chain = ("FIRST",) + tuple(
            f"cursor-{index}"
            for index in range(connector_contract.MAX_PAGE_COUNT - 1)
        )
        with self.assertRaisesRegex(
            connector_contract.ContractError, "continuation exceeds"
        ):
            self.inspect(
                "positions",
                {
                    "positions": [],
                    "next": "https://api.robinhood.com/positions/?cursor=too-many",
                },
                request_cursors=maximum_chain,
            )
        terminal = self.inspect(
            "positions",
            {"positions": []},
            request_cursors=maximum_chain,
        )
        self.assertTrue(terminal["complete"])

    def test_first_positions_projection_uses_validated_connector_fields(self):
        receipt = connector_contract.inspect_first_positions_set(
            [{
                "data": {
                    "positions": [
                        {
                            "symbol": "SPY",
                            "quantity": "1.2500",
                            "intraday_quantity": "-0.25",
                            "average_buy_price": "645.1200",
                            "type": "long",
                            "account_number": "must-not-be-projected",
                        }
                    ]
                }
            }],
            ["first-positions-0"],
            ["FIRST"],
        )
        self.assertEqual(
            receipt["rows"],
            [
                {
                    "symbol": "SPY",
                    "quantity": "1.2500",
                    "intraday_quantity": "-0.25",
                    "average_buy_price": "645.1200",
                }
            ],
        )
        self.assertEqual(receipt["row_count"], len(receipt["rows"]))

    def test_positions_projection_rejects_missing_cost_basis_and_orders(self):
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_first_positions_set(
                [{
                    "data": {
                        "positions": [
                            {
                                "symbol": "SPY",
                                "quantity": "1",
                                "intraday_quantity": "0",
                            }
                        ]
                    }
                }],
                ["first-positions-0"],
                ["FIRST"],
            )

    @staticmethod
    def position(symbol, quantity="1", average_buy_price="10"):
        return {
            "symbol": symbol,
            "quantity": quantity,
            "intraday_quantity": "0",
            "average_buy_price": average_buy_price,
            "type": "long",
        }

    def test_first_positions_set_binds_cursor_chain_and_projects_all_pages(self):
        receipt = connector_contract.inspect_first_positions_set(
            [
                {
                    "data": {
                        "positions": [self.position("SPY")],
                        "next": "https://api.robinhood.com/positions/?cursor=page-2",
                    }
                },
                {
                    "data": {
                        "positions": [
                            self.position("QQQ", "2.5", "400.25")
                        ]
                    }
                },
            ],
            ["first-positions-0", "first-positions-1"],
            ["FIRST", "page-2"],
        )
        self.assertEqual(receipt["action"], "first-positions-set")
        self.assertEqual(receipt["page_count"], 2)
        self.assertEqual(receipt["row_count"], 2)
        self.assertTrue(receipt["complete"])
        self.assertEqual(
            [row["symbol"] for row in receipt["rows"]], ["SPY", "QQQ"]
        )
        self.assertNotIn("output_paths", receipt)

    def test_first_positions_set_rejects_cross_page_duplicate_and_bad_chain(self):
        first = {
            "data": {
                "positions": [self.position("SPY")],
                "next": "https://api.robinhood.com/positions/?cursor=page-2",
            }
        }
        duplicate = {"data": {"positions": [self.position("SPY")]}}
        with self.assertRaisesRegex(
            connector_contract.ContractError, "duplicate position row"
        ):
            connector_contract.inspect_first_positions_set(
                [first, duplicate],
                ["first-positions-0", "first-positions-1"],
                ["FIRST", "page-2"],
            )
        with self.assertRaisesRegex(
            connector_contract.ContractError, "request cursor mismatch"
        ):
            connector_contract.inspect_first_positions_set(
                [first, {"data": {"positions": []}}],
                ["first-positions-0", "first-positions-1"],
                ["FIRST", "wrong"],
            )

    def test_first_positions_set_rejects_non_first_purpose_namespace(self):
        with self.assertRaisesRegex(
            connector_contract.ContractError, "ordered FIRST page namespace"
        ):
            connector_contract.inspect_first_positions_set(
                [{"data": {"positions": []}}],
                ["daily-loss-a-discovery-positions-0"],
                ["FIRST"],
            )

    def test_first_positions_set_accepts_only_exact_retry_name_syntax(self):
        receipt = connector_contract.inspect_first_positions_set(
            [{"data": {"positions": []}}],
            ["first-positions-0-retry"],
            ["FIRST"],
        )
        self.assertTrue(receipt["complete"])
        with self.assertRaisesRegex(
            connector_contract.ContractError, "exact optional -retry suffix"
        ):
            connector_contract.inspect_first_positions_set(
                [{"data": {"positions": []}}],
                ["first-positions-0-anything"],
                ["FIRST"],
            )

    def test_first_positions_set_rejects_repeated_cursor_cycle(self):
        pages = [
            {
                "data": {
                    "positions": [],
                    "next": "https://api.robinhood.com/positions/?cursor=repeat",
                }
            },
            {
                "data": {
                    "positions": [],
                    "next": "https://api.robinhood.com/positions/?cursor=repeat",
                }
            },
            {"data": {"positions": []}},
        ]
        with self.assertRaisesRegex(
            connector_contract.ContractError, "repeated request cursor"
        ):
            connector_contract.inspect_first_positions_set(
                pages,
                [
                    "first-positions-0",
                    "first-positions-1",
                    "first-positions-2",
                ],
                ["FIRST", "repeat", "repeat"],
            )

    def test_rejects_malformed_cursor_page_and_kind(self):
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(
                "positions", {"positions": [], "next": "not-a-cursor-url"}
            )
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(
                "positions",
                {"positions": [{"symbol": "SPY", "quantity": "1"}]},
            )
        with self.assertRaises(connector_contract.ContractError):
            self.inspect("quotes", {"results": []})


class ScanContractTests(unittest.TestCase):
    def inspect(self, *scans):
        return connector_contract.inspect_scan(
            {"data": {"scans": list(scans)}}, "Volume field probe"
        )

    def test_validates_actual_scalar_sorting_field(self):
        receipt = self.inspect(saved_scan())
        self.assertTrue(receipt["found"])
        self.assertTrue(receipt["columns_valid"])
        self.assertTrue(receipt["sort_valid"])
        self.assertFalse(receipt["needs_sort_update"])
        self.assertTrue(receipt["entry_ready"])
        self.assertEqual(receipt["sorting"], "Relative volume desc")

    def test_unsorted_editable_scan_requests_one_sort_update(self):
        receipt = self.inspect(saved_scan(sorting="Last asc"))
        self.assertTrue(receipt["columns_valid"])
        self.assertFalse(receipt["sort_valid"])
        self.assertTrue(receipt["needs_sort_update"])
        self.assertFalse(receipt["entry_ready"])

    def test_cortex_managed_or_missing_column_never_requests_update(self):
        cortex = self.inspect(
            saved_scan(sorting=None, cortex_managed=True)
        )
        self.assertFalse(cortex["needs_sort_update"])

        columns = [
            column
            for column in saved_scan()["columns"]
            if column["display_name"] != "Volume"
        ]
        missing = self.inspect(saved_scan(columns=columns, sorting=None))
        self.assertFalse(missing["columns_valid"])
        self.assertEqual(missing["missing_columns"], ["Volume"])
        self.assertFalse(missing["needs_sort_update"])

    def test_missing_title_is_a_valid_not_found_receipt(self):
        receipt = self.inspect(saved_scan(title="Something else"))
        self.assertFalse(receipt["found"])
        self.assertIsNone(receipt["scan_id"])
        self.assertEqual(
            receipt["missing_columns"],
            list(connector_contract.REQUIRED_SCAN_COLUMNS),
        )

    def test_rejects_ambiguous_title_and_nonscalar_sort(self):
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(saved_scan(), saved_scan(scan_id="second"))
        with self.assertRaises(connector_contract.ContractError):
            self.inspect(saved_scan(sorting={"column": "Relative volume"}))

    def test_scan_update_binds_expected_id_and_authoritative_sorted_by(self):
        receipt = connector_contract.inspect_scan_update(
            {
                "data": {
                    "result": {
                        "scan_id": "scan-id",
                        "sorted_by": "Relative volume desc",
                    }
                }
            },
            "scan-id",
        )
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "action": "scan-update",
                "ok": True,
                "scan_id": "scan-id",
                "sorting": "Relative volume desc",
                "sort_valid": True,
            },
        )

    def test_scan_update_exposes_wrong_sort_but_rejects_wrong_id_or_shape(self):
        wrong_sort = connector_contract.inspect_scan_update(
            {
                "data": {
                    "result": {
                        "scan_id": "scan-id",
                        "sorted_by": "Last asc",
                    }
                }
            },
            "scan-id",
        )
        self.assertFalse(wrong_sort["sort_valid"])
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_scan_update(
                {
                    "data": {
                        "result": {
                            "scan_id": "other-id",
                            "sorted_by": "Relative volume desc",
                        }
                    }
                },
                "scan-id",
            )
        with self.assertRaises(connector_contract.ContractError):
            connector_contract.inspect_scan_update(
                {
                    "data": {
                        "result": {
                            "scan_id": "scan-id",
                            "sorted_by": {"column": "Relative volume"},
                        }
                    }
                },
                "scan-id",
            )


class ConnectorContractCliTests(unittest.TestCase):
    def invoke(self, arguments, document):
        documents = document if isinstance(document, list) else [document]
        validated = [
            (mock.sentinel.path, item, b"{}") for item in documents
        ]
        stdout = io.StringIO()
        with mock.patch.object(
            connector_contract,
            "validate_bound_external_json_purposes",
            return_value=validated,
        ) as validate, mock.patch.object(
            connector_contract,
            "validate_bound_first_positions_request_binding",
        ), mock.patch.object(
            connector_contract,
            "validate_bound_first_positions_page_request_binding",
        ), mock.patch("sys.stdout", stdout):
            status = connector_contract.main(arguments)
        return status, json.loads(stdout.getvalue()), validate

    def test_portfolio_cli_consumes_exact_committed_source_purpose(self):
        document = {
            "structuredContent": portfolio_payload(),
        }
        status, receipt, validate = self.invoke(
            [
                "portfolio",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "first-portfolio",
            ],
            document,
        )
        self.assertEqual(status, 0)
        self.assertTrue(receipt["ok"])
        validate.assert_called_once_with(
            "session-scratch", ["first-portfolio"]
        )

    def test_quote_cli_consumes_the_committed_response_without_raw_probing(self):
        document = {
            "structuredContent": {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "symbol": "SPY",
                                "last_trade_price": "765.18",
                                "previous_close": "765.91",
                            }
                        }
                    ]
                }
            }
        }
        status, receipt, validate = self.invoke(
            [
                "quote",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "spy-red-check",
                "--symbol",
                "SPY",
            ],
            document,
        )
        self.assertEqual(status, 0)
        self.assertTrue(receipt["below_previous_close"])
        self.assertEqual(receipt["change_percent_display"], "-0.10")
        validate.assert_called_once_with(
            "session-scratch", ["spy-red-check"]
        )

    def test_review_cli_consumes_real_envelope_without_runner_schema_guessing(self):
        data = review_payload()["data"]
        document = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"data": data, "guide": "review only"},
                        separators=(",", ":"),
                    ),
                }
            ],
            "structuredContent": {
                "data": data,
                "guide": "review only",
            },
        }
        status, receipt, validate = self.invoke(
            [
                "review",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "buy-review-apyx-0",
                "--symbol",
                "APYX",
                "--side",
                "buy",
                "--order-type",
                "market",
                "--market-hours",
                "regular_hours",
                "--time-in-force",
                "gfd",
                "--dollar-amount",
                "301.79",
            ],
            document,
        )
        self.assertEqual(status, 0, receipt)
        self.assertEqual(receipt["action"], "review")
        self.assertTrue(receipt["clean"])
        self.assertIsNone(receipt["alert_type"])
        self.assertEqual(receipt["order_checks"], {})
        self.assertEqual(receipt["dollar_amount"], "301.79")
        self.assertEqual(receipt["market_hours"], "regular_hours")
        self.assertEqual(receipt["time_in_force"], "gfd")
        self.assertIn("Updated 12:33 PM ET.", receipt["market_data_disclosure"])
        validate.assert_called_once_with(
            "session-scratch", ["buy-review-apyx-0"]
        )

    def test_review_cli_preserves_nested_numeric_checks_as_json_numbers(self):
        document = {
            "structuredContent": review_payload(
                order_checks={
                    "alert_type": "BROKER_TIMING_CHECK",
                    "timing_alert_details": {
                        "seconds": Decimal("2"),
                        "ratio": Decimal("1.25"),
                    },
                }
            )
        }
        status, receipt, _validate = self.invoke(
            [
                "review",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "buy-review-apyx-0",
                "--symbol",
                "APYX",
                "--side",
                "buy",
                "--order-type",
                "market",
                "--market-hours",
                "regular_hours",
                "--time-in-force",
                "gfd",
                "--dollar-amount",
                "301.79",
            ],
            document,
        )
        self.assertEqual(status, 0, receipt)
        self.assertFalse(receipt["clean"])
        details = receipt["order_checks"]["timing_alert_details"]
        self.assertEqual(details["seconds"], 2)
        self.assertEqual(details["ratio"], 1.25)
        self.assertNotIsInstance(details["seconds"], str)

    def test_receipt_stdout_is_ascii_safe_and_json_round_trips_unicode(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            connector_contract._print_json(
                {
                    "disclosure": "Bid × Ask",
                    "details": {"seconds": Decimal("2.50")},
                }
            )
        raw = stdout.getvalue()
        raw.encode("ascii")
        self.assertIn(r"\u00d7", raw)
        self.assertIn('"seconds":2.50', raw)
        parsed = json.loads(raw)
        self.assertEqual(parsed["disclosure"], "Bid × Ask")
        self.assertEqual(parsed["details"]["seconds"], 2.5)

    def test_review_cli_rejects_ambiguous_amount_and_bad_envelopes(self):
        common = [
            "review",
            "--scratch",
            "session-scratch",
            "--source-purpose",
            "buy-review-apyx-0",
            "--symbol",
            "APYX",
            "--side",
            "buy",
            "--order-type",
            "market",
            "--market-hours",
            "regular_hours",
            "--time-in-force",
            "gfd",
        ]
        for amount_arguments in (
            (),
            ("--quantity", "1", "--dollar-amount", "301.79"),
        ):
            with self.subTest(amount_arguments=amount_arguments):
                status, receipt, validate = self.invoke(
                    [*common, *amount_arguments],
                    {"structuredContent": review_payload()},
                )
                self.assertEqual(status, 2)
                self.assertEqual(receipt["error"]["code"], "usage_error")
                validate.assert_not_called()

        bad_documents = (
            {
                "isError": True,
                "content": [{"type": "text", "text": "broker rejected"}],
            },
            {"content": [{"type": "text", "text": "not JSON"}]},
        )
        for document in bad_documents:
            with self.subTest(document=document):
                status, receipt, validate = self.invoke(
                    [*common, "--dollar-amount", "301.79"], document
                )
                self.assertEqual(status, 2)
                self.assertEqual(receipt["error"]["code"], "invalid_contract")
                validate.assert_called_once_with(
                    "session-scratch", ["buy-review-apyx-0"]
                )

    def test_scan_cli_emits_one_compact_receipt(self):
        document = {
            "structuredContent": {"data": {"scans": [saved_scan()]}},
        }
        status, receipt, _validate = self.invoke(
            [
                "scan",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "saved-scans",
                "--title",
                "Volume field probe",
            ],
            document,
        )
        self.assertEqual(status, 0)
        self.assertEqual(receipt["scan_id"], "scan-id")
        self.assertTrue(receipt["entry_ready"])

    def test_page_cli_normalizes_missing_next_from_committed_source(self):
        document = {
            "structuredContent": {"data": {"positions": []}},
        }
        status, receipt, validate = self.invoke(
            [
                "page",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "first-positions-0",
                "--kind",
                "positions",
                "--request-cursor",
                "FIRST",
            ],
            document,
        )
        self.assertEqual(status, 0)
        self.assertTrue(receipt["complete"])
        self.assertIsNone(receipt["next_cursor"])
        self.assertNotIn("rows", receipt)
        self.assertEqual(receipt["source_purpose"], "first-positions-0")
        validate.assert_called_once_with(
            "session-scratch", ["first-positions-0"]
        )

    def test_page_cli_distinguishes_binding_and_pagination_stops(self):
        common = [
            "page", "--scratch", "session-scratch",
            "--source-purpose", "daily-loss-a-discovery-positions-0",
            "--kind", "positions",
        ]
        status, receipt, _validate = self.invoke(
            [
                *common,
                "--request-cursor", "FIRST",
                "--request-cursor", "FIRST",
            ],
            {"structuredContent": {"data": {"positions": []}}},
        )
        self.assertEqual(status, 2)
        self.assertEqual(receipt["error"]["code"], "request_binding_invalid")

        status, receipt, _validate = self.invoke(
            [*common, "--request-cursor", "FIRST"],
            {
                "structuredContent": {
                    "data": {
                        "positions": [],
                        "next": "https://api.robinhood.com/positions/?cursor=FIRST",
                    }
                }
            },
        )
        self.assertEqual(status, 2)
        self.assertEqual(receipt["error"]["code"], "pagination_stopped")

        status, receipt, validate = self.invoke(
            [
                "page", "--scratch", "session-scratch",
                "--source-purpose", "first-positions-0",
                "--kind", "orders", "--request-cursor", "FIRST",
            ],
            {"structuredContent": {"data": {"orders": []}}},
        )
        self.assertEqual(status, 2)
        self.assertEqual(receipt["error"]["code"], "request_binding_invalid")
        validate.assert_not_called()

    def test_page_cli_rejects_cursor_chain_not_bound_by_reservation(self):
        stdout = io.StringIO()
        with mock.patch.object(
            connector_contract,
            "validate_bound_first_positions_page_request_binding",
            side_effect=connector_contract.SourceHandoffError(
                "request_binding_invalid",
                "reservation hash does not bind submitted cursor chain",
            ),
        ), mock.patch("sys.stdout", stdout):
            status = connector_contract.main(
                [
                    "page",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-1",
                    "--kind",
                    "positions",
                    "--request-cursor",
                    "FIRST",
                    "--request-cursor",
                    "invented-cursor",
                ]
            )
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(receipt["error"]["code"], "request_binding_invalid")

    def test_page_cli_rejects_removed_include_rows_option(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            status = connector_contract.main(
                [
                    "page",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-0",
                    "--kind",
                    "positions",
                    "--request-cursor",
                    "FIRST",
                    "--include-rows",
                ]
            )
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(receipt["action"], "page")
        self.assertEqual(receipt["error"]["code"], "usage_error")

    def test_first_positions_set_cli_consumes_all_purposes_in_order(self):
        documents = [
            {
                "structuredContent": {
                    "data": {
                        "positions": [],
                        "next": "https://api.robinhood.com/positions/?cursor=two",
                    }
                }
            },
            {"structuredContent": {"data": {"positions": []}}},
        ]
        status, receipt, validate = self.invoke(
            [
                "first-positions-set",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "first-positions-0",
                "--source-purpose",
                "first-positions-1",
                "--request-cursor",
                "FIRST",
                "--request-cursor",
                "two",
            ],
            documents,
        )
        self.assertEqual(status, 0)
        self.assertEqual(receipt["action"], "first-positions-set")
        self.assertEqual(receipt["rows"], [])
        self.assertNotIn("output_paths", receipt)
        validate.assert_called_once_with(
            "session-scratch",
            ["first-positions-0", "first-positions-1"],
        )

    def test_missing_committed_source_has_distinct_error_code(self):
        stdout = io.StringIO()
        with mock.patch.object(
            connector_contract,
            "validate_bound_external_json_purposes",
            side_effect=connector_contract.SnapshotError("source is missing"),
        ), mock.patch.object(
            connector_contract,
            "validate_bound_first_positions_page_request_binding",
        ), mock.patch("sys.stdout", stdout):
            status = connector_contract.main(
                [
                    "page",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-0",
                    "--kind",
                    "positions",
                    "--request-cursor",
                    "FIRST",
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "source_unavailable",
        )

    def test_source_journal_error_codes_are_preserved(self):
        for code in (
            "source_handoff_pending",
            "source_handoff_aborted",
            "source_journal_invalid",
            "source_file_missing",
        ):
            with self.subTest(code=code):
                stdout = io.StringIO()
                with mock.patch.object(
                    connector_contract,
                    "validate_bound_external_json_purposes",
                    side_effect=connector_contract.SourceHandoffError(
                        code, "source journal diagnostic"
                    ),
                ), mock.patch.object(
                    connector_contract,
                    "validate_bound_first_positions_page_request_binding",
                ), mock.patch("sys.stdout", stdout):
                    status = connector_contract.main(
                        [
                            "page",
                            "--scratch",
                            "session-scratch",
                            "--source-purpose",
                            "first-positions-0",
                            "--kind",
                            "positions",
                            "--request-cursor",
                            "FIRST",
                        ]
                    )
                self.assertEqual(status, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"], code
                )

    def test_retry_source_requires_aborted_connector_failed_base(self):
        document = {"structuredContent": {"data": {"positions": []}}}
        with mock.patch.object(
            connector_contract,
            "validate_bound_source_retry_authorization",
        ) as authorize:
            status, receipt, _validate = self.invoke(
                [
                    "first-positions-set",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-0-retry",
                    "--request-cursor",
                    "FIRST",
                ],
                document,
            )
        self.assertEqual(status, 0, receipt)
        authorize.assert_called_once_with(
            "session-scratch",
            "first-positions-0",
            "first-positions-0-retry",
        )

        stdout = io.StringIO()
        with mock.patch.object(
            connector_contract,
            "validate_bound_source_retry_authorization",
            side_effect=connector_contract.SourceHandoffError(
                "source_retry_not_authorized", "base was not aborted"
            ),
        ), mock.patch.object(
            connector_contract,
            "validate_bound_first_positions_request_binding",
        ), mock.patch("sys.stdout", stdout):
            status = connector_contract.main(
                [
                    "first-positions-set",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-0-retry",
                    "--request-cursor",
                    "FIRST",
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "source_retry_not_authorized",
        )

    def test_retry_page_is_authorized_before_cursor_consumption(self):
        document = {"structuredContent": {"data": {"positions": []}}}
        with mock.patch.object(
            connector_contract,
            "validate_bound_source_retry_authorization",
        ) as authorize:
            status, receipt, _validate = self.invoke(
                [
                    "page",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-0-retry",
                    "--kind",
                    "positions",
                    "--request-cursor",
                    "FIRST",
                ],
                document,
            )
        self.assertEqual(status, 0, receipt)
        authorize.assert_called_once_with(
            "session-scratch",
            "first-positions-0",
            "first-positions-0-retry",
        )

    def test_retry_authorization_infrastructure_error_is_not_semantic(self):
        stdout = io.StringIO()
        with mock.patch.object(
            connector_contract,
            "validate_bound_source_retry_authorization",
            side_effect=connector_contract.SnapshotError("scratch unavailable"),
        ), mock.patch.object(
            connector_contract,
            "validate_bound_first_positions_page_request_binding",
        ), mock.patch("sys.stdout", stdout):
            status = connector_contract.main(
                [
                    "page",
                    "--scratch",
                    "session-scratch",
                    "--source-purpose",
                    "first-positions-0-retry",
                    "--kind",
                    "positions",
                    "--request-cursor",
                    "FIRST",
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "source_unavailable",
        )

    def test_scan_update_cli_uses_committed_response(self):
        document = {
            "structuredContent": {
                "data": {
                    "result": {
                        "scan_id": "scan-id",
                        "sorted_by": "Relative volume desc",
                    }
                }
            },
        }
        status, receipt, validate = self.invoke(
            [
                "scan-update",
                "--scratch",
                "session-scratch",
                "--source-purpose",
                "saved-scan-update",
                "--scan-id",
                "scan-id",
            ],
            document,
        )
        self.assertEqual(status, 0)
        self.assertTrue(receipt["sort_valid"])
        validate.assert_called_once_with(
            "session-scratch", ["saved-scan-update"]
        )

    def test_usage_failure_is_json(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            status = connector_contract.main(["scan"])
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(receipt["action"], "scan")
        self.assertEqual(receipt["error"]["code"], "usage_error")

    def test_orders_set_is_not_a_supported_action(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            status = connector_contract.main(["orders-set"])
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(receipt["action"], "unknown")
        self.assertEqual(receipt["error"]["code"], "usage_error")


if __name__ == "__main__":
    unittest.main()

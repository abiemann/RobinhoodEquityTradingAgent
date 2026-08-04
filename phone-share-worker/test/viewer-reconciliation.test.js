import assert from "node:assert/strict";
import test from "node:test";

import { viewerResponse } from "../src/viewer.js";

function extractFunction(source, name) {
  const start = source.indexOf("function " + name + "(");
  assert.notEqual(start, -1, name + " is present in the embedded viewer");
  const body = source.indexOf("{", start);
  let depth = 0;
  for (let index = body; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error("Could not extract " + name);
}

async function embeddedValidator() {
  const html = await (viewerResponse()).text();
  const helpers = [
    "exactObject", "finite", "boundedText", "numberToCents", "validEtDate", "validatePayload",
  ].map((name) => extractFunction(html, name)).join("\n");
  return Function('"use strict";\n' + helpers + "\nreturn validatePayload;")();
}

async function embeddedReconciliationRenderer() {
  const html = await (viewerResponse()).text();
  const helpers = ["clear", "moneyCents", "renderPnlReconciliation"]
    .map((name) => extractFunction(html, name)).join("\n");
  const root = { firstChild: null, hidden: true, className: "", textContent: "" };
  const render = Function(
    "root",
    '"use strict";\n' + helpers + '\nfunction byId() { return root; }\nreturn renderPnlReconciliation;',
  )(root);
  return { render, root };
}

function legacyPayload() {
  return {
    schema_version: 1,
    captured_at: "2026-08-04T18:00:00.000Z",
    expires_at: "2026-08-04T20:00:00.000Z",
    mode: { dry_run: false },
    snapshot: {
      rules_version: "f8ae9d9",
      run_start_pt: "2026-08-04T11:00:00-07:00",
      session: "regular",
      account: { total_value: 1508.97, cash: 1508.97, buying_power: 891.23 },
      realized_pnl_today: 21.29,
      positions: [],
    },
    runs: [],
    eras: [{
      rules_version: "f8ae9d9", first: "2026-08-04", last: "2026-08-04",
      buys: 1, sells: 2, stops: 0, realized_pnl: 21.2792743172,
    }],
  };
}

function enhancedPayload() {
  const payload = legacyPayload();
  payload.eras = payload.eras.map((era) => ({
    ...era,
    realized_pnl_cents: 2128,
    pnl_quality: "matched-ledger-pool",
  }));
  payload.pnl_reconciliation = {
    date_et: "2026-08-04",
    broker_realized_pnl_cents: 2129,
    strategy_realized_pnl_cents: 2128,
    difference_cents: 1,
    realized_fill_count: 2,
    available_fill_count: 2,
    matched_fill_count: 2,
    status: "difference",
  };
  return payload;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

test("embedded viewer accepts legacy and enhanced schema-v1 P&L payloads", async () => {
  const validate = await embeddedValidator();
  const legacy = legacyPayload();
  assert.equal(validate(legacy, legacy), legacy);
  const enhanced = enhancedPayload();
  assert.equal(validate(enhanced, enhanced), enhanced);
  const unavailableSubtotal = clone(enhancedPayload());
  unavailableSubtotal.eras[0].realized_pnl = 21.29;
  unavailableSubtotal.eras[0].realized_pnl_cents = 2129;
  unavailableSubtotal.eras[0].pnl_quality = "incomplete";
  unavailableSubtotal.pnl_reconciliation.strategy_realized_pnl_cents = 2129;
  unavailableSubtotal.pnl_reconciliation.difference_cents = 0;
  unavailableSubtotal.pnl_reconciliation.available_fill_count = 1;
  unavailableSubtotal.pnl_reconciliation.matched_fill_count = 1;
  unavailableSubtotal.pnl_reconciliation.status = "qualified";
  assert.equal(validate(unavailableSubtotal, unavailableSubtotal), unavailableSubtotal);
  const estimated = clone(enhancedPayload());
  estimated.eras[0].pnl_quality = "estimated";
  estimated.pnl_reconciliation.matched_fill_count = 1;
  estimated.pnl_reconciliation.status = "qualified";
  assert.equal(validate(estimated, estimated), estimated);
  const agrees = clone(enhancedPayload());
  agrees.pnl_reconciliation.strategy_realized_pnl_cents = 2129;
  agrees.pnl_reconciliation.difference_cents = 0;
  agrees.pnl_reconciliation.status = "agrees";
  assert.equal(validate(agrees, agrees), agrees);
});

test("enhanced P&L validation rejects contradictory cents, status, quality, and ET date", async () => {
  const validate = await embeddedValidator();
  const cases = [
    (payload) => { payload.pnl_reconciliation.broker_realized_pnl_cents = 2130; },
    (payload) => { payload.pnl_reconciliation.difference_cents = 0; },
    (payload) => { payload.pnl_reconciliation.status = "agrees"; },
    (payload) => { payload.pnl_reconciliation.date_et = "2026-02-30"; },
    (payload) => { payload.eras[0].realized_pnl_cents = 2129; },
    (payload) => { payload.eras[0].pnl_quality = "tax-basis"; },
    (payload) => { delete payload.pnl_reconciliation.available_fill_count; },
    (payload) => { payload.pnl_reconciliation.available_fill_count = 3; },
    (payload) => {
      payload.pnl_reconciliation.available_fill_count = 0;
      payload.pnl_reconciliation.matched_fill_count = 1;
    },
    (payload) => { payload.pnl_reconciliation.status = "qualified"; },
  ];
  for (const mutate of cases) {
    const payload = clone(enhancedPayload());
    mutate(payload);
    assert.throws(() => validate(payload, payload));
  }
});

test("qualified comparisons never present partial data as agreement", async () => {
  const { render, root } = await embeddedReconciliationRenderer();
  const unavailable = enhancedPayload().pnl_reconciliation;
  unavailable.strategy_realized_pnl_cents = 2129;
  unavailable.difference_cents = 0;
  unavailable.available_fill_count = 1;
  unavailable.matched_fill_count = 1;
  unavailable.status = "qualified";
  render({ pnl_reconciliation: unavailable });
  assert.match(root.textContent, /^Incomplete available strategy subtotal/);
  assert.doesNotMatch(root.textContent, /agree/i);

  const estimated = enhancedPayload().pnl_reconciliation;
  estimated.matched_fill_count = 1;
  estimated.status = "qualified";
  render({ pnl_reconciliation: estimated });
  assert.match(root.textContent, /^Estimated strategy comparison/);
  assert.doesNotMatch(root.textContent, /agree/i);
});

test("legacy viewer copy distinguishes broker and ledger-fill strategy values without tax claims", async () => {
  const html = await (viewerResponse()).text();
  assert.match(html, /Broker realized today/);
  assert.match(html, /Strategy P&amp;L by rules era \(ledger fill basis\)/);
  assert.match(html, /Broker and strategy agree to the cent/);
  assert.match(html, /Broker vs strategy difference/);
  assert.match(html, /Incomplete available strategy subtotal/);
  assert.match(html, /Estimated strategy comparison/);
  assert.match(html, /Broker is authoritative/);
  assert.match(html, /rankEligible:[^\n]+pnl_quality[^\n]+matched-ledger-pool/);
  assert.doesNotMatch(html, /tax[- ]?(?:basis|lot)/i);
});

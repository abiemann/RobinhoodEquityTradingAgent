const VIEWER_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>RHMRA Phone Dashboard</title>
<style nonce="__NONCE__">
  :root { --bg:#14161a; --panel:#1d2026; --line:#2c3037; --text:#d7dae0;
    --dim:#949aa5; --green:#4cc38a; --red:#e5534b; --amber:#d4a72c; --blue:#4c8ac3; }
  * { box-sizing:border-box; }
  html { background:var(--bg); }
  body { margin:0; min-height:100vh; background:var(--bg); color:var(--text);
    font:14px/1.5 system-ui,"Segoe UI",sans-serif;
    padding:calc(16px + env(safe-area-inset-top)) max(12px,env(safe-area-inset-right))
      calc(20px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left)); }
  header { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
  h1 { margin:0 6px 0 0; font-size:19px; font-weight:650; }
  h2 { margin:0 0 10px; color:var(--dim); font-size:12px; font-weight:650;
    letter-spacing:.06em; text-transform:uppercase; }
  section { margin-bottom:12px; padding:14px; overflow:hidden; background:var(--panel);
    border:1px solid var(--line); border-radius:9px; }
  .pill { display:inline-block; padding:2px 9px; color:var(--dim); background:var(--panel);
    border:1px solid var(--line); border-radius:11px; font-size:11px; white-space:nowrap; }
  .pill.live { color:var(--red); border-color:var(--red); transform-origin:center;
    animation:spin-sign 4.5s ease-in-out infinite; }
  .pill.dry { color:var(--green); border-color:var(--green); }
  .pill.warn { color:var(--amber); border-color:var(--amber); }
  @keyframes spin-sign { 0%,55% { transform:perspective(140px) rotateY(0); }
    85%,100% { transform:perspective(140px) rotateY(360deg); } }
  @media (prefers-reduced-motion:reduce) { .pill.live { animation:none; } }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr)); gap:14px; }
  .card .value { font-size:20px; font-weight:650; font-variant-numeric:tabular-nums; }
  .card .key { color:var(--dim); font-size:11px; }
  .pnl-reconciliation { margin-top:12px; padding:9px 10px; color:var(--dim);
    background:#181b20; border:1px solid var(--line); border-radius:7px; font-size:11px; }
  .pnl-reconciliation.ok { color:var(--green); border-color:#285b43; }
  .pnl-reconciliation.warn { color:var(--amber); border-color:#6e5c1d; }
  .positive { color:var(--green); } .negative { color:var(--red); }
  .empty { color:var(--dim); font-style:italic; }
  .scroll { max-width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table { width:100%; border-collapse:collapse; }
  th,td { padding:7px 9px; border-bottom:1px solid var(--line); text-align:right;
    white-space:nowrap; font-variant-numeric:tabular-nums; }
  th { color:var(--dim); font-size:11px; font-weight:550; }
  th:first-child,td:first-child { text-align:left; }
  .stock { color:var(--blue); text-decoration:none; }
  .stock:focus-visible,.stock:hover { text-decoration:underline; }
  .badge { padding:1px 7px; border-radius:8px; font-size:10px; }
  .badge.ok { color:var(--green); background:#1c3529; }
  .badge.bad { color:var(--red); background:#3a1f1d; font-weight:650; }
  .runs { display:flex; gap:6px; flex-wrap:wrap; }
  .run { display:inline-flex; flex-direction:column; align-items:center; gap:2px;
    min-width:70px; padding:5px 9px; color:inherit; background:transparent;
    border:1px solid var(--blue); border-radius:7px; font:inherit; cursor:pointer; }
  .run.halted { border-color:var(--red); }
  .run .time { color:var(--text); font-size:12px; font-weight:650; }
  .run.halted .time { color:var(--red); }
  .run .label { color:var(--dim); font-size:11px; }
  .run:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }
  .run-detail { min-height:21px; margin-top:9px; color:var(--dim); font-size:12px; }
  .stars { margin-right:6px; color:#edc84b; font-size:12px; letter-spacing:1px; white-space:nowrap; }
  button.action { display:block; margin:12px auto 0; padding:6px 12px; color:var(--text);
    background:#25282e; border:1px solid #4a4f58; border-radius:6px; font:inherit; cursor:pointer; }
  button.action:hover { background:#30343b; }
  button.action:focus-visible { outline:2px solid #686e78; outline-offset:2px; }
  .notice { padding:13px; margin-bottom:12px; color:var(--amber); background:#332b18;
    border:1px solid #6e5c1d; border-radius:8px; }
  .notice.error { color:var(--red); background:#3a1f1d; border-color:var(--red); }
  .notice button { margin-top:10px; }
  footer { color:var(--dim); text-align:center; font-size:11px; }
  [hidden] { display:none !important; }
  @media (max-width:540px) {
    body { font-size:13px; }
    h1 { flex-basis:100%; }
    section { padding:12px; }
    .cards { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .card .value { font-size:18px; }
  }
</style>
</head>
<body>
<header>
  <h1>RHMRA Dashboard</h1>
  <span id="mode" class="pill">PHONE</span>
  <span id="freshness" class="pill">loading…</span>
  <span id="rules" class="pill"></span>
  <span id="sync" class="pill"></span>
</header>
<div id="notice" class="notice" hidden></div>
<main id="dashboard" hidden>
  <section><h2>Account</h2><div id="account" class="cards"></div>
    <div id="pnl-reconciliation" class="pnl-reconciliation" hidden></div></section>
  <section><h2>Positions</h2><div id="positions"></div></section>
  <section>
    <h2>Runs today — tap a run for details</h2>
    <div id="runs" class="runs"></div><div id="run-detail" class="run-detail"></div>
  </section>
  <section><h2>Strategy P&amp;L by rules era (ledger fill basis)</h2><div id="eras"></div></section>
</main>
<footer>Read-only • end-to-end encrypted • the decryption key stays in this browser tab</footer>
<script nonce="__NONCE__">
(function () {
  "use strict";
  var ID_KEY = "rhmra.phone.share.id";
  var KEY_KEY = "rhmra.phone.share.key";
  var META_KEY = "rhmra.phone.share.accepted";
  var AUTH_KEY = "rhmra.phone.share.auth-attempted";
  var ID_RE = /^[A-Za-z0-9_-]{22,64}$/;
  var B64_RE = /^[A-Za-z0-9_-]+$/;
  var ISO_RE = /^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$/;
  var POLL_MS = 30000;
  var textEncoder = new TextEncoder();
  var state = { id:null, key:null, etag:null, polling:false, timer:null, accepted:null,
    missingSince:null, eraSignature:null, erasExpanded:false };

  function byId(id) { return document.getElementById(id); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function node(tag, className, text) {
    var result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function exactObject(value, keys) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    var actual = Object.keys(value).sort();
    var wanted = keys.slice().sort();
    return actual.length === wanted.length && actual.every(function (key, i) { return key === wanted[i]; });
  }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function boundedText(value, maximum) { return typeof value === "string" && value.length <= maximum; }
  function canonicalTime(value) {
    if (typeof value !== "string" || !ISO_RE.test(value)) return NaN;
    var millis = Date.parse(value);
    return Number.isFinite(millis) && new Date(millis).toISOString() === value ? millis : NaN;
  }
  function fromBase64Url(value) {
    if (typeof value !== "string" || !B64_RE.test(value) || value.length % 4 === 1) {
      throw new Error("Invalid base64url value");
    }
    var padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4);
    var binary = atob(padded);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }
  function money(value) {
    if (!finite(value)) return "unavailable";
    return (value < 0 ? "-$" : "$") + Math.abs(value).toFixed(2);
  }
  function moneyCents(value, signed) {
    if (!Number.isSafeInteger(value)) return "unavailable";
    var absolute=Math.abs(value), prefix=value<0?"-":signed&&value>0?"+":"";
    return prefix+"$"+Math.floor(absolute/100)+"."+String(absolute%100).padStart(2,"0");
  }
  function numberToCents(value) {
    if (!finite(value)) return null;
    var absolute=Math.round(Math.abs(value)*100);
    return Number.isSafeInteger(absolute) ? (value<0?-absolute:absolute) : null;
  }
  function validEtDate(value) {
    if (typeof value!=="string" || !/^\\d{4}-\\d{2}-\\d{2}$/.test(value)) return false;
    var parsed=Date.parse(value+"T00:00:00Z");
    return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0,10)===value;
  }
  function pnlClass(value) { return value > 0 ? "positive" : value < 0 ? "negative" : ""; }
  function localTime(value) {
    var date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], { hour:"numeric", minute:"2-digit" }) : "unknown";
  }
  function showNotice(message, isError, offerSignIn) {
    var box = byId("notice");
    clear(box);
    box.hidden = false;
    box.className = "notice" + (isError ? " error" : "");
    box.appendChild(document.createTextNode(message));
    if (offerSignIn) {
      var button = node("button", "action", "Sign in again");
      button.type = "button";
      button.addEventListener("click", function () {
        sessionStorage.setItem(AUTH_KEY, "1");
        location.assign("/api/auth");
      });
      box.appendChild(button);
    }
  }
  function hideNotice() { byId("notice").hidden = true; }
  function clearDashboard() {
    byId("dashboard").hidden=true;
    ["account","positions","runs","run-detail","eras","pnl-reconciliation"].forEach(function(id){clear(byId(id));});
    byId("pnl-reconciliation").hidden=true;
  }
  function forgetShare() {
    sessionStorage.removeItem(ID_KEY);
    sessionStorage.removeItem(KEY_KEY);
    sessionStorage.removeItem(META_KEY);
    sessionStorage.removeItem(AUTH_KEY);
  }
  function acceptQrFragment() {
    if (!location.hash) return false;
    var params = new URLSearchParams(location.hash.slice(1));
    var id = params.get("id") || "";
    var key = params.get("key") || "";
    var keyBytes;
    try { keyBytes = fromBase64Url(key); } catch (_) { keyBytes = null; }
    if (!ID_RE.test(id) || !keyBytes || keyBytes.byteLength !== 32 || params.size !== 2) {
      try { forgetShare(); } catch (_) { /* invalid fragment remains unusable */ }
      try { history.replaceState(null, "", "/view"); } catch (_) { /* best effort */ }
      showNotice("This QR link is invalid. Generate a new share from the laptop dashboard.", true, false);
      return true;
    }
    try {
      sessionStorage.setItem(ID_KEY, id);
      sessionStorage.setItem(KEY_KEY, key);
      sessionStorage.removeItem(META_KEY);
      sessionStorage.setItem(AUTH_KEY, "1");
      if (sessionStorage.getItem(ID_KEY) !== id || sessionStorage.getItem(KEY_KEY) !== key) {
        throw new Error("session storage read-back failed");
      }
    } catch (_) {
      showNotice("This browser could not safely save the QR decryption key. The key remains in the address bar; do not close this tab. Enable temporary session storage, then reload this page.", true, false);
      return true;
    }
    try { history.replaceState(null, "", "/view"); } catch (_) { /* navigation below still removes it */ }
    location.replace("/api/auth");
    return true;
  }
  function loadAccepted(id) {
    try {
      var value = JSON.parse(sessionStorage.getItem(META_KEY) || "null");
      if (!exactObject(value, ["share_id","sequence","captured_at","expires_at","cipher_hash"]) ||
          value.share_id !== id || !Number.isSafeInteger(value.sequence) || value.sequence < 1 ||
          !boundedText(value.cipher_hash, 64)) return null;
      return value;
    } catch (_) { return null; }
  }
  function validateEnvelope(value) {
    var keys = ["schema_version","share_id","sequence","captured_at","expires_at","iv","ciphertext"];
    if (!exactObject(value, keys) || value.schema_version !== 1 || value.share_id !== state.id ||
        !Number.isSafeInteger(value.sequence) || value.sequence < 1) throw new Error("Invalid encrypted envelope");
    var captured = canonicalTime(value.captured_at);
    var expires = canonicalTime(value.expires_at);
    if (!Number.isFinite(captured) || !Number.isFinite(expires) || expires <= captured ||
        expires - captured > 28800000 || captured > Date.now() + 120000) throw new Error("Invalid envelope timestamps");
    var iv = fromBase64Url(value.iv);
    var ciphertext = fromBase64Url(value.ciphertext);
    if (iv.byteLength !== 12 || ciphertext.byteLength < 16 || ciphertext.byteLength > 262144) {
      throw new Error("Invalid encrypted payload size");
    }
    return { value:value, capturedMs:captured, expiresMs:expires, iv:iv, ciphertext:ciphertext };
  }
  function validatePayload(value, envelope) {
    var legacyTopKeys = ["schema_version","captured_at","expires_at","mode","snapshot","runs","eras"];
    var enhancedTopKeys = legacyTopKeys.concat(["pnl_reconciliation"]);
    var enhanced = Object.prototype.hasOwnProperty.call(value || {}, "pnl_reconciliation");
    var topKeys = enhanced ? enhancedTopKeys : legacyTopKeys;
    if (!exactObject(value, topKeys) || value.schema_version !== 1 ||
        value.captured_at !== envelope.captured_at || value.expires_at !== envelope.expires_at ||
        !exactObject(value.mode, ["dry_run"]) || typeof value.mode.dry_run !== "boolean") {
      throw new Error("Decrypted snapshot schema is invalid");
    }
    var snap = value.snapshot;
    var snapKeys = ["rules_version","run_start_pt","session","account","realized_pnl_today","positions"];
    if (!exactObject(snap, snapKeys) || !boundedText(snap.rules_version, 128) ||
        !boundedText(snap.run_start_pt, 64) || !Number.isFinite(Date.parse(snap.run_start_pt)) ||
        Date.parse(snap.run_start_pt) > Date.parse(value.captured_at) + 120000 ||
        !boundedText(snap.session, 32) || !finite(snap.realized_pnl_today) ||
        !exactObject(snap.account, ["total_value","cash","buying_power"]) ||
        ![snap.account.total_value,snap.account.cash,snap.account.buying_power].every(finite) ||
        !Array.isArray(snap.positions) || snap.positions.length > 100) throw new Error("Snapshot fields are invalid");
    snap.positions.forEach(function (position) {
      var fields = ["symbol","quantity","avg_buy_price","current_price","stop_price","stop_state"];
      if (!exactObject(position, fields) || typeof position.symbol !== "string" ||
          !/^[A-Z0-9.-]{1,12}$/.test(position.symbol) || !finite(position.quantity) || position.quantity < 0 ||
          !finite(position.avg_buy_price) || position.avg_buy_price < 0 ||
          !finite(position.current_price) || position.current_price < 0 ||
          !(position.stop_price === null || (finite(position.stop_price) && position.stop_price >= 0)) ||
          !boundedText(position.stop_state, 32)) {
        throw new Error("Position fields are invalid");
      }
    });
    if (!Array.isArray(value.runs) || value.runs.length > 100) throw new Error("Runs are invalid");
    value.runs.forEach(function (run) {
      if (!exactObject(run, ["time","label","phase","tooltip"]) || !boundedText(run.time, 32) ||
          !boundedText(run.label, 80) || !boundedText(run.phase, 32) || !boundedText(run.tooltip, 500)) {
        throw new Error("Run fields are invalid");
      }
    });
    if (!Array.isArray(value.eras) || value.eras.length > 500) throw new Error("Rules eras are invalid");
    value.eras.forEach(function (era) {
      var legacyFields = ["rules_version","first","last","buys","sells","stops","realized_pnl"];
      var enhancedFields = legacyFields.concat(["realized_pnl_cents","pnl_quality"]);
      if (!exactObject(era, enhanced ? enhancedFields : legacyFields) ||
          !boundedText(era.rules_version, 128) || !boundedText(era.first, 16) || !boundedText(era.last, 16) ||
          ![era.buys,era.sells,era.stops].every(function (n) { return Number.isSafeInteger(n) && n >= 0; }) ||
          !finite(era.realized_pnl) || (enhanced &&
            (!Number.isSafeInteger(era.realized_pnl_cents) ||
             numberToCents(era.realized_pnl)!==era.realized_pnl_cents ||
             ["matched-ledger-pool","estimated","incomplete"].indexOf(era.pnl_quality)===-1))) {
        throw new Error("Rules-era fields are invalid");
      }
    });
    if (enhanced) {
      var reconciliation=value.pnl_reconciliation;
      var reconciliationFields=["date_et","broker_realized_pnl_cents","strategy_realized_pnl_cents",
        "difference_cents","realized_fill_count","available_fill_count","matched_fill_count","status"];
      if (!exactObject(reconciliation,reconciliationFields) || !validEtDate(reconciliation.date_et) ||
          ![reconciliation.broker_realized_pnl_cents,reconciliation.strategy_realized_pnl_cents,
            reconciliation.difference_cents].every(Number.isSafeInteger) ||
          ![reconciliation.realized_fill_count,reconciliation.available_fill_count,
            reconciliation.matched_fill_count].every(function(n){
            return Number.isSafeInteger(n) && n>=0;
          }) || reconciliation.matched_fill_count>reconciliation.available_fill_count ||
          reconciliation.available_fill_count>reconciliation.realized_fill_count ||
          reconciliation.broker_realized_pnl_cents!==numberToCents(snap.realized_pnl_today) ||
          reconciliation.difference_cents!==reconciliation.broker_realized_pnl_cents-
            reconciliation.strategy_realized_pnl_cents ||
          ["agrees","difference","qualified"].indexOf(reconciliation.status)===-1 ||
          reconciliation.status!==(reconciliation.matched_fill_count<reconciliation.realized_fill_count
            ? "qualified" : reconciliation.difference_cents===0 ? "agrees" : "difference")) {
        throw new Error("P&L reconciliation fields are invalid");
      }
    }
    return value;
  }
  async function sha256(value) {
    var digest = await crypto.subtle.digest("SHA-256", textEncoder.encode(value));
    return Array.from(new Uint8Array(digest)).map(function (b) { return b.toString(16).padStart(2,"0"); }).join("");
  }
  async function checkedAcceptance(envelope) {
    var hash = await sha256(envelope.iv+"."+envelope.ciphertext);
    var previous = state.accepted;
    if (previous) {
      if (envelope.sequence < previous.sequence ||
          (envelope.sequence === previous.sequence &&
           (envelope.captured_at !== previous.captured_at || envelope.expires_at !== previous.expires_at ||
            hash !== previous.cipher_hash)) ||
          (envelope.sequence > previous.sequence && Date.parse(envelope.captured_at) < Date.parse(previous.captured_at)) ||
          envelope.expires_at !== previous.expires_at) throw new Error("A stale or rolled-back snapshot was rejected");
    }
    var accepted = { share_id:state.id, sequence:envelope.sequence, captured_at:envelope.captured_at,
      expires_at:envelope.expires_at, cipher_hash:hash };
    return accepted;
  }
  function persistAcceptance(accepted) {
    sessionStorage.setItem(META_KEY, JSON.stringify(accepted));
    state.accepted=accepted;
  }
  async function decryptEnvelope(parsed) {
    var envelope = parsed.value;
    var aad = textEncoder.encode(JSON.stringify([
      envelope.schema_version, envelope.share_id, envelope.sequence, envelope.captured_at, envelope.expires_at
    ]));
    var clearBytes = await crypto.subtle.decrypt(
      { name:"AES-GCM", iv:parsed.iv, additionalData:aad, tagLength:128 }, state.key, parsed.ciphertext
    );
    var clearText = new TextDecoder("utf-8", { fatal:true }).decode(clearBytes);
    return validatePayload(JSON.parse(clearText), envelope);
  }
  function appendCard(parent, label, value, className) {
    var card = node("div", "card");
    card.appendChild(node("div", "value " + (className || ""), value));
    card.appendChild(node("div", "key", label));
    parent.appendChild(card);
  }
  function renderAccount(payload) {
    var snap = payload.snapshot;
    var brokerCents=payload.pnl_reconciliation &&
      payload.pnl_reconciliation.broker_realized_pnl_cents;
    var unrealized = snap.positions.reduce(function (sum, p) {
      return sum + (p.current_price - p.avg_buy_price) * p.quantity;
    }, 0);
    var account = byId("account"); clear(account);
    appendCard(account, "Total value", money(snap.account.total_value));
    appendCard(account, "Cash", money(snap.account.cash));
    appendCard(account, "Buying power", money(snap.account.buying_power));
    appendCard(account, "Broker realized today",
      Number.isSafeInteger(brokerCents)?moneyCents(brokerCents):money(snap.realized_pnl_today),
      pnlClass(snap.realized_pnl_today));
    appendCard(account, "Unrealized", money(unrealized), pnlClass(unrealized));
  }
  function renderPnlReconciliation(payload) {
    var root=byId("pnl-reconciliation"), item=payload.pnl_reconciliation;
    clear(root);
    if(!item){root.hidden=true;return;}
    root.hidden=false;
    if(item.status==="qualified"){
      root.className="pnl-reconciliation warn";
      if(item.available_fill_count<item.realized_fill_count){
        root.textContent="Incomplete available strategy subtotal · broker "+
          moneyCents(item.broker_realized_pnl_cents)+" · available strategy subtotal "+
          moneyCents(item.strategy_realized_pnl_cents)+" · difference "+
          moneyCents(item.difference_cents,true)+" · "+item.available_fill_count+" of "+
          item.realized_fill_count+" realized fills available, "+item.matched_fill_count+
          " matched. Broker is authoritative.";
      }else{
        root.textContent="Estimated strategy comparison · broker "+
          moneyCents(item.broker_realized_pnl_cents)+" · strategy estimate "+
          moneyCents(item.strategy_realized_pnl_cents)+" · difference "+
          moneyCents(item.difference_cents,true)+" · "+item.matched_fill_count+" of "+
          item.realized_fill_count+" realized fills matched. Broker is authoritative.";
      }
      return;
    }
    if(item.status==="agrees"){
      root.className="pnl-reconciliation ok";
      root.textContent="Broker and strategy agree to the cent · "+
        moneyCents(item.broker_realized_pnl_cents);
      return;
    }
    root.className="pnl-reconciliation warn";
    root.textContent="Broker vs strategy difference · broker "+
      moneyCents(item.broker_realized_pnl_cents)+" · strategy "+
      moneyCents(item.strategy_realized_pnl_cents)+" · difference "+
      moneyCents(item.difference_cents,true)+". Broker is authoritative.";
  }
  function cell(row, text, className) { var td=node("td",className||"",text); row.appendChild(td); return td; }
  function renderPositions(positions) {
    var root=byId("positions"); clear(root);
    if (!positions.length) { root.appendChild(node("span","empty","account is flat — no open positions")); return; }
    var wrap=node("div","scroll"), table=node("table"), header=node("tr");
    ["Symbol","Qty","Avg buy","Current","Unrealized","Stop","To stop"].forEach(function (value) {
      header.appendChild(node("th","",value));
    });
    table.appendChild(header);
    positions.forEach(function (p) {
      var row=node("tr"), first=cell(row,"");
      var link=node("a","stock",p.symbol);
      link.href="https://robinhood.com/stocks/"+encodeURIComponent(p.symbol)+"?source=lists_section_position";
      link.target="_blank"; link.rel="noopener noreferrer"; first.appendChild(link);
      var pnl=(p.current_price-p.avg_buy_price)*p.quantity;
      var pct=p.avg_buy_price > 0 ? (p.current_price/p.avg_buy_price-1)*100 : 0;
      cell(row,String(p.quantity)); cell(row,"$"+p.avg_buy_price.toFixed(4)); cell(row,"$"+p.current_price.toFixed(4));
      cell(row,money(pnl)+" ("+pct.toFixed(2)+"%)",pnlClass(pnl));
      var protectedStop=p.stop_price !== null && (p.stop_state === "confirmed" || p.stop_state === "queued");
      var stopCell=cell(row,"");
      stopCell.appendChild(node("span","badge "+(protectedStop?"ok":"bad"),
        protectedStop ? p.stop_state+" @ $"+p.stop_price.toFixed(2) : "UNPROTECTED"));
      var distance=protectedStop && p.current_price > 0 ? ((p.current_price-p.stop_price)/p.current_price*100).toFixed(2)+"%" : "—";
      cell(row,distance); table.appendChild(row);
    });
    wrap.appendChild(table); root.appendChild(wrap);
  }
  function renderRuns(runs) {
    var root=byId("runs"), detail=byId("run-detail"); clear(root); detail.textContent="";
    if (!runs.length) { root.appendChild(node("span","empty","no runs in this snapshot")); return; }
    runs.forEach(function (run) {
      var button=node("button","run"+(run.phase==="halted"?" halted":"")); button.type="button"; button.title=run.tooltip;
      button.appendChild(node("span","time",run.time)); button.appendChild(node("span","label",run.label));
      button.addEventListener("click",function(){ detail.textContent=run.time+" — "+run.tooltip; });
      root.appendChild(button);
    });
  }
  function renderEras(eras) {
    var root=byId("eras"), signature=JSON.stringify(eras.map(function(e){return [e.rules_version,e.first,e.last];}));
    if (state.eraSignature !== null && signature !== state.eraSignature) state.erasExpanded=false;
    state.eraSignature=signature; clear(root);
    if (!eras.length) { root.appendChild(node("span","empty","no ledger data")); return; }
    var ranked=eras.map(function(e,index){return {version:e.rules_version,
      profit:Number.isSafeInteger(e.realized_pnl_cents)?e.realized_pnl_cents:e.realized_pnl,index:index,
      rankEligible:!Object.prototype.hasOwnProperty.call(e,"pnl_quality")||e.pnl_quality==="matched-ledger-pool"};})
      .filter(function(e){return e.rankEligible&&e.profit>0;}).sort(function(a,b){return b.profit-a.profit||a.index-b.index;}).slice(0,3);
    var stars=new Map(ranked.map(function(e,index){return [e.version,3-index];}));
    var visible=state.erasExpanded?eras:eras.slice(0,12), wrap=node("div","scroll"), table=node("table"), header=node("tr");
    ["rules_version","Dates","Buys","Sells","STOPs","Strategy P&L"].forEach(function(value){header.appendChild(node("th","",value));});
    table.appendChild(header);
    visible.forEach(function(e){
      var row=node("tr"); cell(row,e.rules_version); cell(row,e.first===e.last?e.first:e.first+" → "+e.last);
      cell(row,String(e.buys)); cell(row,String(e.sells)); cell(row,String(e.stops));
      var cents=Number.isSafeInteger(e.realized_pnl_cents)?e.realized_pnl_cents:null;
      var profit=cell(row,"",pnlClass(cents===null?e.realized_pnl:cents)), count=stars.get(e.rules_version)||0;
      if(count){var mark=node("span","stars","★".repeat(count)); mark.title=(count===3?"Largest":count===2?"Second-largest":"Third-largest")+" strategy profit"; profit.appendChild(mark);}
      var estimate=e.pnl_quality==="estimated"?"~":"";
      profit.appendChild(document.createTextNode(estimate+(cents===null?money(e.realized_pnl):moneyCents(cents))));
      if(e.pnl_quality==="incomplete"){
        var missing=node("span","empty"," + unavailable");
        missing.title="One or more sells have no matched ledger acquisition pool";
        profit.appendChild(missing);
      }
      table.appendChild(row);
    });
    wrap.appendChild(table); root.appendChild(wrap);
    var remaining=eras.length-visible.length;
    if(remaining>0){var button=node("button","action","Click to show "+remaining+" more...");button.type="button";
      button.addEventListener("click",function(){state.erasExpanded=true;renderEras(eras);});root.appendChild(button);}
  }
  function render(payload) {
    hideNotice(); byId("dashboard").hidden=false;
    var mode=byId("mode"); mode.textContent=payload.mode.dry_run?"DRY":"LIVE"; mode.className="pill "+(payload.mode.dry_run?"dry":"live");
    byId("rules").textContent="rules "+payload.snapshot.rules_version;
    var age=Math.max(0,Math.round((Date.now()-Date.parse(payload.snapshot.run_start_pt))/60000));
    var freshness=byId("freshness"); freshness.textContent="snapshot "+age+" min old ("+payload.snapshot.session+")";
    freshness.className="pill"+(age>45?" warn":"");
    byId("sync").textContent="received "+localTime(payload.captured_at);
    renderAccount(payload); renderPnlReconciliation(payload); renderPositions(payload.snapshot.positions);
    renderRuns(payload.runs); renderEras(payload.eras);
  }
  function schedule(expiresMs) {
    clearTimeout(state.timer);
    var remaining=expiresMs-Date.now();
    if(remaining<=0){clearDashboard();showNotice("This phone share has expired. Generate a new one from the laptop dashboard.",false,false);forgetShare();return;}
    state.timer=setTimeout(poll,Math.min(POLL_MS,remaining+100));
  }
  async function poll() {
    if(state.polling)return; state.polling=true;
    try {
      var headers={"Accept":"application/json"}; if(state.etag)headers["If-None-Match"]=state.etag;
      var response;
      try {
        response=await fetch("/api/shares/"+encodeURIComponent(state.id),{method:"GET",headers:headers,cache:"no-store",credentials:"same-origin",redirect:"manual"});
      } catch (_) {
        showNotice("The secure service could not be reached. Your sign-in may have expired.",true,true);
        if(state.accepted)schedule(Date.parse(state.accepted.expires_at));
        return;
      }
      if(response.type==="opaqueredirect"||response.status===401||response.status===403){showNotice("Your secure viewing session needs authentication.",false,true);return;}
      if(response.status===304){byId("sync").textContent="checked "+new Date().toLocaleTimeString();schedule(Date.parse(state.accepted.expires_at));return;}
      if(response.status===410){clearDashboard();showNotice("This phone share has expired or was stopped from the laptop.",false,false);forgetShare();return;}
      if(response.status===404){
        clearDashboard();
        if(state.missingSince===null)state.missingSince=Date.now();
        if(Date.now()-state.missingSince<600000){
          showNotice("The first encrypted snapshot is not available yet. The phone will retry automatically.",false,false);
          clearTimeout(state.timer);
          state.timer=setTimeout(poll,POLL_MS);
        }else showNotice("This phone share is unavailable. Check the laptop or generate a new share.",true,false);
        return;
      }
      if((response.headers.get("Content-Type")||"").split(";",1)[0]!=="application/json"){
        showNotice("The secure service returned an authentication page. Sign in again to continue.",true,true);
        if(state.accepted)schedule(Date.parse(state.accepted.expires_at));
        return;
      }
      if(!response.ok)throw new Error("Secure snapshot request failed ("+response.status+")");
      state.missingSince=null;
      var envelope=(await response.json()), parsed=validateEnvelope(envelope);
      if(parsed.expiresMs<=Date.now()){clearDashboard();showNotice("This phone share has expired.",false,false);forgetShare();return;}
      var accepted=await checkedAcceptance(envelope);
      var payload=await decryptEnvelope(parsed);
      persistAcceptance(accepted); render(payload);
      state.etag=response.headers.get("ETag"); schedule(parsed.expiresMs);
    } catch(error) {
      showNotice("The encrypted dashboard could not be refreshed: "+(error&&error.message?error.message:"unknown error"),true,false);
      if(state.accepted)schedule(Date.parse(state.accepted.expires_at));
    } finally {state.polling=false;}
  }
  async function start() {
    if(acceptQrFragment())return;
    var id=sessionStorage.getItem(ID_KEY)||"", keyText=sessionStorage.getItem(KEY_KEY)||"";
    var keyBytes; try{keyBytes=fromBase64Url(keyText);}catch(_){keyBytes=null;}
    if(!ID_RE.test(id)||!keyBytes||keyBytes.byteLength!==32){showNotice("Scan the QR code shown by your laptop dashboard to begin.",false,false);return;}
    state.id=id; state.accepted=loadAccepted(id);
    try{state.key=await crypto.subtle.importKey("raw",keyBytes,{name:"AES-GCM"},false,["decrypt"]);}catch(_){forgetShare();showNotice("The QR decryption key is invalid.",true,false);return;}
    poll();
  }
  document.addEventListener("visibilitychange",function(){if(!document.hidden&&state.id)poll();});
  start().catch(function(){
    clearDashboard();
    showNotice("This browser blocked the temporary session storage needed to protect the QR key.",true,false);
  });
}());
</script>
</body>
</html>`;

function cspNonce() {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary);
}

export function viewerResponse() {
  const nonce = cspNonce();
  const headers = new Headers({
    "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
    "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'`,
    "Content-Type": "text/html; charset=utf-8",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
  });
  return new Response(VIEWER_HTML.replaceAll("__NONCE__", nonce), { status: 200, headers });
}

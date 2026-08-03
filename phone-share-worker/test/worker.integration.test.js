import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

import { Miniflare } from "miniflare";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const UPLOAD_TOKEN = "integration-test-upload-token-32-characters";

function makeEnvelope(shareId, sequence, capturedAt, expiresAt, overrides = {}) {
  return {
    schema_version: 1,
    share_id: shareId,
    sequence,
    captured_at: capturedAt,
    expires_at: expiresAt,
    iv: "A".repeat(16),
    ciphertext: "B".repeat(22),
    ...overrides,
  };
}

async function withMiniflare(callback) {
  const mf = new Miniflare({
    compatibilityDate: "2026-08-03",
    modules: true,
    modulesRules: [{ type: "ESModule", include: ["**/*.js"] }],
    modulesRoot: ROOT,
    scriptPath: path.join(ROOT, "src", "index.js"),
    durableObjects: {
      SHARE_SESSION: { className: "ShareSession", useSQLite: true },
    },
    bindings: {
      WRITE_AUTH_MODE: "bearer",
      VERIFY_ACCESS_JWT: "false",
      UPLOAD_TOKEN,
      MAX_BODY_BYTES: "393216",
      MAX_CIPHERTEXT_BYTES: "262144",
      MAX_TTL_SECONDS: "7200",
      MAX_CAPTURE_AGE_SECONDS: "900",
      CLOCK_SKEW_SECONDS: "120",
      TOMBSTONE_SECONDS: "3600",
    },
  });
  try {
    await callback(mf);
  } finally {
    await mf.dispose();
  }
}

function apiRequest(mf, method, shareId, body, headers = {}) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  if (method === "PUT" || method === "DELETE") {
    init.headers.Authorization = `Bearer ${UPLOAD_TOKEN}`;
  }
  return mf.dispatchFetch(`http://localhost/api/shares/${shareId}`, init);
}

async function assertStatus(response, expected) {
  const detail = response.status === expected ? undefined : await response.clone().text();
  assert.equal(response.status, expected, detail);
}

test("SQLite share state machine handles retry, update, cache, revoke, and expiry", async () => {
  await withMiniflare(async (mf) => {
    const shareId = "AbCdEfGhIjKlMnOpQrStUv";
    let response = await apiRequest(mf, "GET", shareId);
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error, "share_not_found");

    const capturedAt = new Date().toISOString();
    const expiresAt = new Date(Date.parse(capturedAt) + 2 * 60 * 60 * 1000).toISOString();
    const first = makeEnvelope(shareId, 1, capturedAt, expiresAt);

    response = await apiRequest(mf, "PUT", shareId, first);
    await assertStatus(response, 201);
    assert.deepEqual(await response.json(), {
      accepted_sequence: 1,
      expires_at: expiresAt,
      status: "active",
    });

    response = await apiRequest(mf, "PUT", shareId, first);
    await assertStatus(response, 200);
    assert.deepEqual(await response.json(), {
      accepted_sequence: 1,
      duplicate: true,
      expires_at: expiresAt,
      status: "active",
    });

    const changedAtSameSequence = { ...first, ciphertext: "C".repeat(22) };
    response = await apiRequest(mf, "PUT", shareId, changedAtSameSequence);
    assert.equal(response.status, 409);
    assert.equal((await response.json()).error, "rollback_rejected");

    const secondCapturedAt = new Date(Date.parse(capturedAt) + 1).toISOString();
    const second = makeEnvelope(shareId, 2, secondCapturedAt, expiresAt, {
      iv: "D".repeat(16), ciphertext: "E".repeat(22),
    });
    response = await apiRequest(mf, "PUT", shareId, second);
    await assertStatus(response, 200);
    assert.equal((await response.json()).accepted_sequence, 2);

    response = await apiRequest(mf, "GET", shareId);
    await assertStatus(response, 200);
    assert.equal(response.headers.get("Cache-Control").includes("no-store"), true);
    assert.equal(response.headers.get("ETag"), '"rhmra-2"');
    assert.deepEqual(await response.json(), second);

    response = await apiRequest(mf, "GET", shareId, undefined, { "If-None-Match": '"rhmra-2"' });
    assert.equal(response.status, 304);
    assert.equal(await response.text(), "");

    response = await apiRequest(mf, "DELETE", shareId);
    await assertStatus(response, 204);
    response = await apiRequest(mf, "GET", shareId);
    assert.equal(response.status, 410);
    assert.equal((await response.json()).error, "share_revoked");

    const expiringId = "ZyXwVuTsRqPoNmLkJiHgFe";
    const expiringCapturedAt = new Date().toISOString();
    const expiringAt = new Date(Date.parse(expiringCapturedAt) + 1500).toISOString();
    response = await apiRequest(mf, "PUT", expiringId,
      makeEnvelope(expiringId, 1, expiringCapturedAt, expiringAt));
    await assertStatus(response, 201);
    await new Promise((resolve) => setTimeout(resolve, 1750));
    response = await apiRequest(mf, "GET", expiringId);
    assert.equal(response.status, 410);
    assert.equal((await response.json()).error, "share_expired");
    response = await apiRequest(mf, "PUT", expiringId,
      makeEnvelope(expiringId, 2, new Date().toISOString(),
        new Date(Date.now() + 1500).toISOString()));
    assert.equal(response.status, 410);
    assert.equal((await response.json()).error, "share_expired");
  });
});

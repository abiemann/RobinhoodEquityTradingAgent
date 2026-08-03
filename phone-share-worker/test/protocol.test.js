import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import test from "node:test";

import {
  ProtocolError,
  constantTimeEqual,
  limitsFromEnv,
  validateEnvelope,
  validateShareId,
} from "../src/protocol.js";
import { isLoopbackRequest, requireCloudflareAccess } from "../src/access.js";

const NOW = Date.parse("2026-08-03T18:00:00.000Z");
const SHARE_ID = "AbCdEfGhIjKlMnOpQrStUv";

function envelope(overrides = {}) {
  return {
    schema_version: 1,
    share_id: SHARE_ID,
    sequence: 1,
    captured_at: new Date(NOW).toISOString(),
    expires_at: new Date(NOW + 2 * 60 * 60 * 1000).toISOString(),
    iv: "A".repeat(16),
    ciphertext: "B".repeat(22),
    ...overrides,
  };
}

function expectProtocolError(callback, status, code) {
  assert.throws(callback, (error) => {
    assert.ok(error instanceof ProtocolError);
    assert.equal(error.status, status);
    assert.equal(error.code, code);
    return true;
  });
}

test("default limits enforce a two-hour maximum share", () => {
  const limits = limitsFromEnv({});
  assert.equal(limits.maxTtlMs, 2 * 60 * 60 * 1000);
  assert.equal(limits.maxCiphertextBytes, 262144);
});

test("configured lifetime can increase only to eight hours", () => {
  assert.equal(limitsFromEnv({ MAX_TTL_SECONDS: "28800" }).maxTtlMs, 8 * 60 * 60 * 1000);
  expectProtocolError(() => limitsFromEnv({ MAX_TTL_SECONDS: "28801" }), 500, "invalid_worker_configuration");
});

test("valid encrypted envelope is normalized without plaintext fields", () => {
  const result = validateEnvelope(envelope(), SHARE_ID, NOW, limitsFromEnv({}));
  assert.equal(result.sequence, 1);
  assert.equal(result.ciphertextBytes, 16);
  assert.equal(result.expiresAtMs, NOW + 2 * 60 * 60 * 1000);
});

test("envelope rejects unknown or missing fields", () => {
  expectProtocolError(
    () => validateEnvelope(envelope({ account: { cash: 1 } }), SHARE_ID, NOW, limitsFromEnv({})),
    422, "invalid_envelope",
  );
  const missing = envelope();
  delete missing.iv;
  expectProtocolError(
    () => validateEnvelope(missing, SHARE_ID, NOW, limitsFromEnv({})),
    422, "invalid_envelope",
  );
});

test("envelope binds its opaque ID to the request path", () => {
  expectProtocolError(
    () => validateEnvelope(envelope({ share_id: "Z".repeat(22) }), SHARE_ID, NOW, limitsFromEnv({})),
    422, "share_id_mismatch",
  );
});

test("share IDs require at least 128 bits of base64url-shaped entropy", () => {
  assert.equal(validateShareId(SHARE_ID), SHARE_ID);
  expectProtocolError(() => validateShareId("too-short"), 400, "invalid_share_id");
  expectProtocolError(() => validateShareId("A".repeat(21) + "/"), 400, "invalid_share_id");
});

test("AES-GCM envelope requires a 12-byte IV and authentication tag", () => {
  expectProtocolError(
    () => validateEnvelope(envelope({ iv: "A".repeat(15) }), SHARE_ID, NOW, limitsFromEnv({})),
    422, "invalid_iv",
  );
  expectProtocolError(
    () => validateEnvelope(envelope({ ciphertext: "B".repeat(20) }), SHARE_ID, NOW, limitsFromEnv({})),
    413, "invalid_ciphertext_size",
  );
});

test("stale, future, expired, and overlong envelopes fail closed", () => {
  const limits = limitsFromEnv({});
  expectProtocolError(
    () => validateEnvelope(envelope({ captured_at: new Date(NOW - 901000).toISOString() }), SHARE_ID, NOW, limits),
    422, "stale_capture",
  );
  expectProtocolError(
    () => validateEnvelope(envelope({ captured_at: new Date(NOW + 121000).toISOString() }), SHARE_ID, NOW, limits),
    422, "future_capture",
  );
  expectProtocolError(
    () => validateEnvelope(envelope({ expires_at: new Date(NOW).toISOString() }), SHARE_ID, NOW, limits),
    422, "invalid_expiry",
  );
  expectProtocolError(
    () => validateEnvelope(envelope({ expires_at: new Date(NOW + 7200001).toISOString() }), SHARE_ID, NOW, limits),
    422, "ttl_too_long",
  );
});

test("an already-matched envelope can be revalidated after the upload freshness window", () => {
  const retryNow = NOW + 16 * 60 * 1000;
  const result = validateEnvelope(envelope(), SHARE_ID, retryNow, limitsFromEnv({}), {
    allowStaleCapture: true,
  });
  assert.equal(result.sequence, 1);
});

test("token comparison handles unequal values without prefix acceptance", () => {
  assert.equal(constantTimeEqual("x".repeat(32), "x".repeat(32)), true);
  assert.equal(constantTimeEqual("x".repeat(32), "x".repeat(31)), false);
  assert.equal(constantTimeEqual("x".repeat(31) + "a", "x".repeat(31) + "b"), false);
});

test("Access verification bypass is explicit and production config fails closed", async () => {
  assert.equal(await requireCloudflareAccess(new Request("http://127.0.0.1:8787/api/auth"), {
    VERIFY_ACCESS_JWT: "false",
  }), null);
  await assert.rejects(
    requireCloudflareAccess(new Request("https://rhmra.example/api/auth"), {
      VERIFY_ACCESS_JWT: "false",
    }),
    (error) => error instanceof ProtocolError && error.status === 503 && error.code === "unsafe_local_auth_override",
  );
  await assert.rejects(
    requireCloudflareAccess(new Request("https://example.test/api/auth"), { VERIFY_ACCESS_JWT: "true" }),
    (error) => error instanceof ProtocolError && error.status === 503 && error.code === "invalid_access_configuration",
  );
});

test("local authentication overrides recognize only explicit loopback hosts", () => {
  for (const url of [
    "http://localhost:8787/api/auth",
    "https://dev.localhost/api/auth",
    "http://127.0.0.1/api/auth",
    "http://[::1]:8787/api/auth",
  ]) assert.equal(isLoopbackRequest(new Request(url)), true, url);
  for (const url of [
    "https://localhost.example/api/auth",
    "https://127.0.0.2/api/auth",
    "https://rhmra.workers.dev/api/auth",
    "https://example.com/api/auth",
  ]) assert.equal(isLoopbackRequest(new Request(url)), false, url);
});

test("Access verifier accepts only a correctly signed issuer and audience", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const publicJwk = publicKey.export({ format: "jwk" });
  Object.assign(publicJwk, { alg: "RS256", kid: "rhmra-test-key", use: "sig" });
  const issuer = "https://rhmra-test.cloudflareaccess.com";
  const audience = "0123456789abcdef0123456789abcdef";
  const encode = (value) => Buffer.from(typeof value === "string" ? value : JSON.stringify(value))
    .toString("base64url");
  const header = encode({ alg: "RS256", kid: publicJwk.kid, typ: "JWT" });
  const payload = encode({
    type: "app", iss: issuer, aud: [audience], iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + 300, common_name: "uploader-client.access", sub: "",
  });
  const signedPart = `${header}.${payload}`;
  const signature = sign("RSA-SHA256", Buffer.from(signedPart), privateKey).toString("base64url");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    assert.equal(url, `${issuer}/cdn-cgi/access/certs`);
    assert.equal(init.redirect, "manual");
    return new Response(JSON.stringify({ keys: [publicJwk] }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const claims = await requireCloudflareAccess(new Request("https://worker.test/api/auth", {
      headers: { "Cf-Access-Jwt-Assertion": `${signedPart}.${signature}` },
    }), { VERIFY_ACCESS_JWT: "true", ACCESS_TEAM_DOMAIN: issuer, ACCESS_AUD: audience });
    assert.equal(claims.common_name, "uploader-client.access");

    const alteredSignature = (signature[0] === "A" ? "B" : "A") + signature.slice(1);
    await assert.rejects(
      requireCloudflareAccess(new Request("https://worker.test/api/auth", {
        headers: { "Cf-Access-Jwt-Assertion": `${signedPart}.${alteredSignature}` },
      }), { VERIFY_ACCESS_JWT: "true", ACCESS_TEAM_DOMAIN: issuer, ACCESS_AUD: audience }),
      (error) => error instanceof ProtocolError && error.status === 401,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

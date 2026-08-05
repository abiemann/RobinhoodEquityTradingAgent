import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/index.js";

const CLIENT_ID = "13490783057-78kr2v2lluafbeomf9d1f2u24b2mpv1c.apps.googleusercontent.com";
const GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
const SECRET = "test-secret-held-only-by-worker";
const SCOPE = "https://www.googleapis.com/auth/drive.appdata";
const ALLOWING_LIMITER = Object.freeze({
  async limit() {
    return { success: true };
  },
});
const ENV = Object.freeze({
  GOOGLE_DESKTOP_CLIENT_ID: CLIENT_ID,
  GOOGLE_DESKTOP_CLIENT_SECRET: SECRET,
  OAUTH_RATE_LIMITER: ALLOWING_LIMITER,
});

function request(fields, init = {}) {
  const body = fields instanceof URLSearchParams ? fields.toString() : String(fields);
  return new Request(init.url || "https://broker.example/oauth/token", {
    method: init.method || "POST",
    headers: {
      "Content-Type": init.contentType || "application/x-www-form-urlencoded",
      ...(init.headers || {}),
    },
    body: init.method === "GET" ? undefined : body,
  });
}

function authorizationFields(overrides = {}) {
  return new URLSearchParams({
    grant_type: "authorization_code",
    client_id: CLIENT_ID,
    code: "authorization-code-value",
    code_verifier: "A".repeat(43),
    redirect_uri: "http://127.0.0.1:9000/oauth2/callback",
    ...overrides,
  });
}

function googleJson(value, status = 200, headers = {}, url = GOOGLE_TOKEN_ENDPOINT) {
  const response = new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
  });
  Object.defineProperty(response, "url", { value: url });
  return response;
}

async function json(response) {
  return JSON.parse(await response.text());
}

test("rate limiter runs before a valid request reaches Google", async () => {
  const events = [];
  const limiterCalls = [];
  const env = {
    ...ENV,
    OAUTH_RATE_LIMITER: {
      async limit(options) {
        events.push("limit");
        limiterCalls.push(options);
        return { success: true };
      },
    },
  };
  const response = await handleRequest(request(authorizationFields(), {
    headers: { "cf-connecting-ip": "203.0.113.7" },
  }), env, {
    fetchImpl: async () => {
      events.push("fetch");
      return googleJson({
        access_token: "access-token",
        expires_in: 3600,
        scope: SCOPE,
        token_type: "Bearer",
      });
    },
  });

  assert.equal(response.status, 200);
  assert.deepEqual(limiterCalls, [{ key: "203.0.113.7" }]);
  assert.deepEqual(events, ["limit", "fetch"]);
});

test("rate-limited requests return 429 and never reach Google", async () => {
  let fetched = false;
  const response = await handleRequest(request(authorizationFields()), {
    ...ENV,
    OAUTH_RATE_LIMITER: {
      async limit() {
        return { success: false };
      },
    },
  }, {
    fetchImpl: async () => {
      fetched = true;
      return googleJson({});
    },
  });

  assert.equal(response.status, 429);
  assert.equal((await json(response)).error, "rate_limited");
  assert.equal(response.headers.get("Retry-After"), "60");
  assert.match(response.headers.get("Cache-Control"), /no-store/);
  assert.equal(fetched, false);
});

test("missing rate-limit binding fails closed before Google", async () => {
  let fetched = false;
  const response = await handleRequest(request(authorizationFields()), {
    GOOGLE_DESKTOP_CLIENT_ID: CLIENT_ID,
    GOOGLE_DESKTOP_CLIENT_SECRET: SECRET,
  }, {
    fetchImpl: async () => {
      fetched = true;
      return googleJson({});
    },
  });

  assert.equal(response.status, 503);
  assert.equal((await json(response)).error, "broker_not_configured");
  assert.equal(fetched, false);
});

test("authorization-code relay pins Google and adds the secret only upstream", async () => {
  let captured;
  const response = await handleRequest(request(authorizationFields()), ENV, {
    fetchImpl: async (url, init) => {
      captured = { url, init };
      return googleJson({
        access_token: "access-token",
        expires_in: 3600,
        refresh_token: "refresh-token",
        scope: SCOPE,
        token_type: "Bearer",
        id_token: "must-not-be-forwarded",
        unexpected: "must-not-be-forwarded",
      });
    },
  });

  assert.equal(response.status, 200);
  assert.equal(captured.url, GOOGLE_TOKEN_ENDPOINT);
  assert.equal(captured.init.method, "POST");
  assert.equal(captured.init.redirect, "manual");
  assert.ok(captured.init.signal instanceof AbortSignal);
  const forwarded = new URLSearchParams(captured.init.body);
  assert.equal(forwarded.get("client_id"), CLIENT_ID);
  assert.equal(forwarded.get("client_secret"), SECRET);
  assert.equal(forwarded.get("code"), "authorization-code-value");
  assert.equal(forwarded.get("code_verifier"), "A".repeat(43));
  assert.equal(forwarded.get("redirect_uri"), "http://127.0.0.1:9000/oauth2/callback");
  assert.deepEqual(await json(response), {
    access_token: "access-token",
    expires_in: 3600,
    token_type: "Bearer",
    refresh_token: "refresh-token",
    scope: SCOPE,
  });
  assert.match(response.headers.get("Cache-Control"), /no-store/);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
});

test("refresh relay accepts only the refresh grant fields", async () => {
  let forwarded;
  const fields = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: CLIENT_ID,
    refresh_token: "refresh-token-value",
  });
  const response = await handleRequest(request(fields), ENV, {
    fetchImpl: async (_url, init) => {
      forwarded = new URLSearchParams(init.body);
      return googleJson({
        access_token: "new-access-token",
        expires_in: 3600,
        scope: SCOPE,
        token_type: "Bearer",
      });
    },
  });

  assert.equal(response.status, 200);
  assert.equal(forwarded.get("refresh_token"), "refresh-token-value");
  assert.equal(forwarded.get("client_secret"), SECRET);
  assert.equal(forwarded.has("code"), false);
});

test("caller-supplied client secrets and extra or duplicate fields are rejected", async () => {
  let called = false;
  const fetchImpl = async () => {
    called = true;
    return googleJson({});
  };

  const withSecret = authorizationFields();
  withSecret.set("client_secret", "caller-secret");
  assert.equal((await handleRequest(request(withSecret), ENV, { fetchImpl })).status, 400);

  const withExtra = authorizationFields();
  withExtra.set("scope", SCOPE);
  assert.equal((await handleRequest(request(withExtra), ENV, { fetchImpl })).status, 400);

  const duplicate = authorizationFields().toString() + "&code=second-code";
  assert.equal((await handleRequest(request(duplicate), ENV, { fetchImpl })).status, 400);
  assert.equal(called, false);
});

test("client id, PKCE verifier, and exact loopback redirect are validated", async () => {
  const fetchImpl = async () => {
    throw new Error("must not be called");
  };
  assert.equal((await handleRequest(request(authorizationFields({
    client_id: "other.apps.googleusercontent.com",
  })), ENV, { fetchImpl })).status, 400);
  assert.equal((await handleRequest(request(authorizationFields({
    code_verifier: "too-short",
  })), ENV, { fetchImpl })).status, 400);

  for (const redirect_uri of [
    "http://localhost:9000/oauth2/callback",
    "https://127.0.0.1:9000/oauth2/callback",
    "http://127.0.0.1:80/oauth2/callback",
    "http://127.0.0.1:9000/oauth2/callback?extra=1",
    "http://127.0.0.1:9000/other",
  ]) {
    const response = await handleRequest(request(authorizationFields({ redirect_uri })), ENV, { fetchImpl });
    assert.equal(response.status, 400, redirect_uri);
  }
});

test("only the exact POST route and URL-encoded identity bodies are accepted", async () => {
  const getResponse = await handleRequest(new Request("https://broker.example/oauth/token"), ENV);
  assert.equal(getResponse.status, 405);
  assert.equal(getResponse.headers.get("Allow"), "POST");
  assert.equal(getResponse.headers.get("Access-Control-Allow-Origin"), null);

  assert.equal((await handleRequest(request(authorizationFields(), {
    url: "https://broker.example/other",
  }), ENV)).status, 404);
  assert.equal((await handleRequest(request(authorizationFields(), {
    url: "https://broker.example/oauth/token?debug=1",
  }), ENV)).status, 400);
  assert.equal((await handleRequest(request("{}", {
    contentType: "application/json",
  }), ENV)).status, 415);
  assert.equal((await handleRequest(request(authorizationFields(), {
    headers: { "Content-Encoding": "gzip" },
  }), ENV)).status, 415);
});

test("oversized request bodies and missing production configuration fail closed", async () => {
  const oversized = "x=" + "A".repeat(16 * 1024);
  assert.equal((await handleRequest(request(oversized), ENV)).status, 413);
  assert.equal((await handleRequest(request(authorizationFields()), {
    GOOGLE_DESKTOP_CLIENT_ID: CLIENT_ID,
    OAUTH_RATE_LIMITER: ALLOWING_LIMITER,
  })).status, 503);
});

test("Google OAuth errors are sanitized without changing actionable 4xx status", async () => {
  const response = await handleRequest(request(authorizationFields()), ENV, {
    fetchImpl: async () => googleJson({
      error: "invalid_grant",
      error_description: "Bad or expired code",
      internal_debug_token: "must-not-be-forwarded",
    }, 400),
  });
  assert.equal(response.status, 400);
  assert.deepEqual(await json(response), {
    error: "invalid_grant",
    error_description: "Bad or expired code",
  });
});

test("Google redirects and unexpected final URLs fail closed", async () => {
  let fetchCalls = 0;
  let redirectMode;
  const redirected = await handleRequest(request(authorizationFields()), ENV, {
    fetchImpl: async (_url, init) => {
      fetchCalls += 1;
      redirectMode = init.redirect;
      const response = new Response(null, {
        status: 302,
        headers: { Location: "https://attacker.example/collect" },
      });
      Object.defineProperty(response, "url", { value: GOOGLE_TOKEN_ENDPOINT });
      return response;
    },
  });

  assert.equal(redirectMode, "manual");
  assert.equal(fetchCalls, 1);
  assert.equal(redirected.status, 502);
  assert.equal((await json(redirected)).error, "invalid_upstream_response");

  const unexpectedUrl = await handleRequest(request(authorizationFields()), ENV, {
    fetchImpl: async () => googleJson({
      error: "invalid_grant",
      error_description: "Bad code",
    }, 400, {}, "https://attacker.example/token"),
  });
  assert.equal(unexpectedUrl.status, 502);
  assert.equal((await json(unexpectedUrl)).error, "invalid_upstream_response");
});

test("malformed upstream responses fail closed", async () => {
  const htmlResponse = await handleRequest(request(authorizationFields()), ENV, {
    fetchImpl: async () => new Response("<html>failure</html>", {
      status: 502,
      headers: { "Content-Type": "text/html" },
    }),
  });
  assert.equal(htmlResponse.status, 502);
  assert.equal((await json(htmlResponse)).error, "invalid_upstream_response");

  const unsafeSuccess = await handleRequest(request(authorizationFields()), ENV, {
    fetchImpl: async () => googleJson({
      access_token: "token\nwith-newline",
      expires_in: 3600,
      token_type: "Bearer",
    }),
  });
  assert.equal(unsafeSuccess.status, 502);
});

test("upstream timeout is bounded and does not expose request secrets", async () => {
  const response = await handleRequest(request(authorizationFields()), ENV, {
    timeoutMs: 5,
    fetchImpl: async (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    }),
  });
  assert.equal(response.status, 504);
  assert.deepEqual(await json(response), {
    error: "upstream_timeout",
    error_description: "Google did not respond in time.",
  });
});

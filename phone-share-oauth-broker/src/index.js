const TOKEN_PATH = "/oauth/token";
const GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
const DRIVE_APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata";
const MAX_REQUEST_BYTES = 16 * 1024;
const MAX_UPSTREAM_BYTES = 32 * 1024;
const DEFAULT_TIMEOUT_MS = 10_000;

const BASE_HEADERS = Object.freeze({
  "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
  "Pragma": "no-cache",
  "Referrer-Policy": "no-referrer",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
});

class BrokerError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "BrokerError";
    this.status = status;
    this.code = code;
  }
}

function responseWithHeaders(response, extra = {}) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(BASE_HEADERS)) headers.set(name, value);
  for (const [name, value] of Object.entries(extra)) headers.set(name, value);
  headers.delete("Access-Control-Allow-Credentials");
  headers.delete("Access-Control-Allow-Headers");
  headers.delete("Access-Control-Allow-Methods");
  headers.delete("Access-Control-Allow-Origin");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function jsonResponse(value, status = 200, extra = {}) {
  return responseWithHeaders(new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  }), extra);
}

function errorResponse(error) {
  if (error instanceof BrokerError) {
    const extraHeaders = error.status === 429 && error.code === "rate_limited"
      ? { "Retry-After": "60" } : {};
    return jsonResponse(
      { error: error.code, error_description: error.message },
      error.status,
      extraHeaders,
    );
  }
  return jsonResponse({
    error: "internal_error",
    error_description: "The OAuth broker could not complete the request.",
  }, 500);
}

function requireConfiguredEnvironment(env) {
  const clientId = typeof env?.GOOGLE_DESKTOP_CLIENT_ID === "string"
    ? env.GOOGLE_DESKTOP_CLIENT_ID.trim() : "";
  const clientSecret = typeof env?.GOOGLE_DESKTOP_CLIENT_SECRET === "string"
    ? env.GOOGLE_DESKTOP_CLIENT_SECRET.trim() : "";
  if (!clientId || clientId.length > 512 || !clientId.endsWith(".apps.googleusercontent.com")) {
    throw new BrokerError(503, "broker_not_configured", "The OAuth broker is not configured.");
  }
  if (!clientSecret || clientSecret.length > 512) {
    throw new BrokerError(503, "broker_not_configured", "The OAuth broker is not configured.");
  }
  return { clientId, clientSecret };
}

async function enforceRateLimit(request, env) {
  const limiter = env?.OAUTH_RATE_LIMITER;
  if (!limiter || typeof limiter.limit !== "function") {
    throw new BrokerError(503, "broker_not_configured", "The OAuth broker is not configured.");
  }
  const forwardedIp = (request.headers.get("cf-connecting-ip") || "").trim();
  const key = forwardedIp.length >= 2 && forwardedIp.length <= 64
    && /^[0-9A-Fa-f:.]+$/.test(forwardedIp)
    ? forwardedIp.toLowerCase()
    : "unknown-client";
  let result;
  try {
    result = await limiter.limit({ key });
  } catch {
    throw new BrokerError(503, "broker_unavailable", "The OAuth broker is unavailable.");
  }
  if (!result || result.success !== true) {
    if (result?.success === false) {
      throw new BrokerError(429, "rate_limited", "Too many OAuth requests. Try again later.");
    }
    throw new BrokerError(503, "broker_unavailable", "The OAuth broker is unavailable.");
  }
}

async function readFormBody(request) {
  const contentType = (request.headers.get("Content-Type") || "")
    .split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/x-www-form-urlencoded") {
    throw new BrokerError(415, "unsupported_media_type",
      "Content-Type must be application/x-www-form-urlencoded.");
  }
  const encoding = (request.headers.get("Content-Encoding") || "identity").trim().toLowerCase();
  if (encoding !== "identity") {
    throw new BrokerError(415, "unsupported_content_encoding",
      "Compressed request bodies are not accepted.");
  }

  const declaredLength = request.headers.get("Content-Length");
  if (declaredLength !== null) {
    if (!/^\d+$/.test(declaredLength)) {
      throw new BrokerError(400, "invalid_content_length", "Content-Length is invalid.");
    }
    if (Number(declaredLength) > MAX_REQUEST_BYTES) {
      throw new BrokerError(413, "body_too_large", "Request body exceeds the allowed size.");
    }
  }
  if (!request.body) {
    throw new BrokerError(400, "empty_body", "A form request body is required.");
  }

  const reader = request.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let bytesRead = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytesRead += value.byteLength;
      if (bytesRead > MAX_REQUEST_BYTES) {
        await reader.cancel();
        throw new BrokerError(413, "body_too_large", "Request body exceeds the allowed size.");
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
  } catch (error) {
    if (error instanceof BrokerError) throw error;
    throw new BrokerError(400, "invalid_body", "Request body is not valid UTF-8.");
  }

  if (!text || /[^\x20-\x7e]/.test(text) || /%(?![0-9A-Fa-f]{2})/.test(text)) {
    throw new BrokerError(400, "invalid_form", "Request body is not a valid URL-encoded form.");
  }

  const fields = new Map();
  for (const [name, value] of new URLSearchParams(text)) {
    if (fields.has(name)) {
      throw new BrokerError(400, "duplicate_parameter", "Form parameters must not be repeated.");
    }
    fields.set(name, value);
  }
  if (fields.has("client_secret")) {
    throw new BrokerError(400, "client_secret_not_allowed",
      "The desktop client secret must not be supplied by callers.");
  }
  return fields;
}

function requireExactFields(fields, expected) {
  if (fields.size !== expected.size) {
    throw new BrokerError(400, "invalid_parameters", "The OAuth request parameters are invalid.");
  }
  for (const name of fields.keys()) {
    if (!expected.has(name)) {
      throw new BrokerError(400, "invalid_parameters", "The OAuth request parameters are invalid.");
    }
  }
}

function safeOpaqueValue(value, minimum, maximum) {
  return typeof value === "string" && value.length >= minimum && value.length <= maximum
    && !/[\x00-\x20\x7f-\x9f]/.test(value);
}

function validateLoopbackRedirect(value) {
  if (typeof value !== "string") return false;
  const match = /^http:\/\/127\.0\.0\.1:(\d{1,5})\/oauth2\/callback$/.exec(value);
  if (!match) return false;
  const port = Number(match[1]);
  return Number.isInteger(port) && port >= 1024 && port <= 65535;
}

function validateAndBuildUpstreamForm(fields, configured) {
  const clientId = fields.get("client_id");
  if (clientId !== configured.clientId) {
    throw new BrokerError(400, "invalid_client", "The OAuth client is not accepted by this broker.");
  }

  const grantType = fields.get("grant_type");
  if (grantType === "authorization_code") {
    requireExactFields(fields, new Set([
      "grant_type", "client_id", "code", "code_verifier", "redirect_uri",
    ]));
    if (!safeOpaqueValue(fields.get("code"), 8, 4096)) {
      throw new BrokerError(400, "invalid_grant", "The authorization code is invalid.");
    }
    const verifier = fields.get("code_verifier");
    if (typeof verifier !== "string" || !/^[A-Za-z0-9._~-]{43,128}$/.test(verifier)) {
      throw new BrokerError(400, "invalid_request", "The PKCE code verifier is invalid.");
    }
    if (!validateLoopbackRedirect(fields.get("redirect_uri"))) {
      throw new BrokerError(400, "invalid_request", "The OAuth redirect URI is invalid.");
    }
  } else if (grantType === "refresh_token") {
    requireExactFields(fields, new Set(["grant_type", "client_id", "refresh_token"]));
    if (!safeOpaqueValue(fields.get("refresh_token"), 8, 8192)) {
      throw new BrokerError(400, "invalid_grant", "The refresh token is invalid.");
    }
  } else {
    throw new BrokerError(400, "unsupported_grant_type",
      "Only authorization_code and refresh_token grants are accepted.");
  }

  const upstream = new URLSearchParams();
  for (const [name, value] of fields) upstream.set(name, value);
  upstream.set("client_secret", configured.clientSecret);
  return upstream;
}

function safeResponseToken(value, maximum = 8192) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum
    && !/[\x00-\x20\x7f-\x9f]/.test(value);
}

function safeErrorText(value, maximum) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum
    && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value);
}

function sanitizeGoogleResponse(status, document) {
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }

  if (status >= 200 && status < 300) {
    if (!safeResponseToken(document.access_token)
      || document.token_type !== "Bearer"
      || !Number.isInteger(document.expires_in)
      || document.expires_in < 60 || document.expires_in > 86400
      || (document.refresh_token !== undefined && !safeResponseToken(document.refresh_token))
      || (document.scope !== undefined && document.scope !== DRIVE_APPDATA_SCOPE)) {
      throw new BrokerError(502, "invalid_upstream_response",
        "Google returned an invalid OAuth response.");
    }
    const clean = {
      access_token: document.access_token,
      expires_in: document.expires_in,
      token_type: "Bearer",
    };
    if (document.refresh_token !== undefined) clean.refresh_token = document.refresh_token;
    if (document.scope !== undefined) clean.scope = document.scope;
    return { status, document: clean };
  }

  const clean = {
    error: safeErrorText(document.error, 128) ? document.error : "oauth_error",
  };
  if (safeErrorText(document.error_description, 512)) {
    clean.error_description = document.error_description;
  }
  if (safeErrorText(document.error_uri, 512)) clean.error_uri = document.error_uri;
  const cleanStatus = status >= 400 && status <= 499 ? status : 502;
  return { status: cleanStatus, document: clean };
}

async function readGoogleResponse(response) {
  const declaredLength = response.headers.get("Content-Length");
  if (declaredLength !== null && /^\d+$/.test(declaredLength)
    && Number(declaredLength) > MAX_UPSTREAM_BYTES) {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }
  const contentType = (response.headers.get("Content-Type") || "")
    .split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > MAX_UPSTREAM_BYTES) {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }
  let document;
  try {
    document = JSON.parse(text);
  } catch {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }
  return sanitizeGoogleResponse(response.status, document);
}

async function relayToGoogle(form, fetchImpl, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetchImpl(GOOGLE_TOKEN_ENDPOINT, {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: form.toString(),
      redirect: "manual",
      signal: controller.signal,
    });
  } catch {
    if (controller.signal.aborted) {
      throw new BrokerError(504, "upstream_timeout", "Google did not respond in time.");
    }
    throw new BrokerError(502, "upstream_unavailable", "Google OAuth is temporarily unavailable.");
  } finally {
    clearTimeout(timer);
  }
  if (response.url !== GOOGLE_TOKEN_ENDPOINT
      || (response.status >= 300 && response.status <= 399)) {
    throw new BrokerError(502, "invalid_upstream_response",
      "Google returned an invalid OAuth response.");
  }
  const sanitized = await readGoogleResponse(response);
  return jsonResponse(sanitized.document, sanitized.status);
}

export async function handleRequest(request, env, options = {}) {
  try {
    const url = new URL(request.url);
    if (url.pathname !== TOKEN_PATH) {
      return jsonResponse({ error: "not_found", error_description: "Resource not found." }, 404);
    }
    if (url.search) {
      throw new BrokerError(400, "query_not_allowed", "Query parameters are not accepted.");
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "method_not_allowed", error_description: "Method not allowed." }, 405, {
        "Allow": "POST",
      });
    }

    await enforceRateLimit(request, env);
    const configured = requireConfiguredEnvironment(env);
    const fields = await readFormBody(request);
    const upstreamForm = validateAndBuildUpstreamForm(fields, configured);
    const fetchImpl = options.fetchImpl || globalThis.fetch;
    if (typeof fetchImpl !== "function") {
      throw new BrokerError(503, "broker_not_configured", "The OAuth broker is not configured.");
    }
    const timeoutMs = Number.isInteger(options.timeoutMs) && options.timeoutMs > 0
      ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
    return await relayToGoogle(upstreamForm, fetchImpl, timeoutMs);
  } catch (error) {
    return errorResponse(error);
  }
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};

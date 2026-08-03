import { ProtocolError } from "./protocol.js";

const BASE64URL_RE = /^[A-Za-z0-9_-]+$/;
const JWKS_CACHE_MS = 60 * 60 * 1000;
const CLOCK_TOLERANCE_SECONDS = 60;
const jwksCache = new Map();

export function isLoopbackRequest(request) {
  let hostname;
  try {
    hostname = new URL(request.url).hostname.toLowerCase();
  } catch {
    return false;
  }
  return hostname === "localhost" || hostname.endsWith(".localhost")
    || hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
}

function accessError(code = "invalid_access_token") {
  return new ProtocolError(401, code, "A valid Cloudflare Access session is required.");
}

function decodeBase64Url(value) {
  if (typeof value !== "string" || !BASE64URL_RE.test(value) || value.length % 4 === 1) {
    throw accessError();
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4);
  let binary;
  try {
    binary = atob(padded);
  } catch {
    throw accessError();
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function decodeJsonPart(value) {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(decodeBase64Url(value)));
  } catch (error) {
    if (error instanceof ProtocolError) throw error;
    throw accessError();
  }
}

function accessConfig(request, env) {
  if (env.VERIFY_ACCESS_JWT === "false") {
    if (!isLoopbackRequest(request)) {
      throw new ProtocolError(503, "unsafe_local_auth_override", "Local authentication overrides cannot be used on a deployed Worker.");
    }
    return null;
  }
  if (env.VERIFY_ACCESS_JWT !== undefined && env.VERIFY_ACCESS_JWT !== "true") {
    throw new ProtocolError(503, "invalid_access_configuration", "Cloudflare Access verification is misconfigured.");
  }
  const rawTeamDomain = typeof env.ACCESS_TEAM_DOMAIN === "string" ? env.ACCESS_TEAM_DOMAIN.trim() : "";
  const audience = typeof env.ACCESS_AUD === "string" ? env.ACCESS_AUD.trim() : "";
  let url;
  try {
    url = new URL(rawTeamDomain);
  } catch {
    throw new ProtocolError(503, "invalid_access_configuration", "Cloudflare Access verification is not configured.");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port ||
      url.pathname !== "/" || url.search || url.hash || !url.hostname.endsWith(".cloudflareaccess.com") ||
      audience.length < 16 || audience.length > 256) {
    throw new ProtocolError(503, "invalid_access_configuration", "Cloudflare Access verification is not configured safely.");
  }
  return { issuer: url.origin, audience, certsUrl: `${url.origin}/cdn-cgi/access/certs` };
}

async function loadJwks(config, forceRefresh = false) {
  const cached = jwksCache.get(config.issuer);
  if (!forceRefresh && cached && cached.expiresAt > Date.now()) return cached.keys;

  let response;
  try {
    response = await fetch(config.certsUrl, {
      method: "GET",
      headers: { "Accept": "application/json" },
      redirect: "error",
    });
  } catch {
    throw new ProtocolError(503, "access_keys_unavailable", "Cloudflare Access signing keys are unavailable.");
  }
  if (!response.ok) {
    throw new ProtocolError(503, "access_keys_unavailable", "Cloudflare Access signing keys are unavailable.");
  }
  const text = await response.text();
  if (text.length > 65536) {
    throw new ProtocolError(503, "access_keys_invalid", "Cloudflare Access signing keys are invalid.");
  }
  let document;
  try {
    document = JSON.parse(text);
  } catch {
    throw new ProtocolError(503, "access_keys_invalid", "Cloudflare Access signing keys are invalid.");
  }
  if (!document || !Array.isArray(document.keys) || document.keys.length < 1 || document.keys.length > 20) {
    throw new ProtocolError(503, "access_keys_invalid", "Cloudflare Access signing keys are invalid.");
  }
  const keys = document.keys.filter((key) => key && key.kty === "RSA" && key.use === "sig"
    && key.alg === "RS256" && typeof key.kid === "string" && key.kid.length <= 256);
  if (!keys.length) {
    throw new ProtocolError(503, "access_keys_invalid", "Cloudflare Access signing keys are invalid.");
  }
  jwksCache.set(config.issuer, { keys, expiresAt: Date.now() + JWKS_CACHE_MS });
  return keys;
}

function validateClaims(payload, config, nowSeconds) {
  const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (payload.iss !== config.issuer || !audiences.includes(config.audience) ||
      typeof payload.exp !== "number" || !Number.isFinite(payload.exp) ||
      payload.exp <= nowSeconds - CLOCK_TOLERANCE_SECONDS ||
      (payload.nbf !== undefined && (typeof payload.nbf !== "number" ||
        payload.nbf > nowSeconds + CLOCK_TOLERANCE_SECONDS)) ||
      (payload.iat !== undefined && (typeof payload.iat !== "number" ||
        payload.iat > nowSeconds + CLOCK_TOLERANCE_SECONDS))) {
    throw accessError();
  }
}

async function verifyWithKey(jwt, key) {
  try {
    const cryptoKey = await crypto.subtle.importKey(
      "jwk", key, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"],
    );
    return await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5", cryptoKey, jwt.signature,
      new TextEncoder().encode(`${jwt.encodedHeader}.${jwt.encodedPayload}`),
    );
  } catch {
    return false;
  }
}

export async function requireCloudflareAccess(request, env) {
  const config = accessConfig(request, env);
  if (config === null) return null;
  const token = request.headers.get("Cf-Access-Jwt-Assertion") || "";
  const parts = token.split(".");
  if (parts.length !== 3 || token.length > 16384) throw accessError("missing_access_token");

  const header = decodeJsonPart(parts[0]);
  const payload = decodeJsonPart(parts[1]);
  const signature = decodeBase64Url(parts[2]);
  if (!header || header.alg !== "RS256" || typeof header.kid !== "string" ||
      header.kid.length < 1 || header.kid.length > 256 || !payload || typeof payload !== "object") {
    throw accessError();
  }
  validateClaims(payload, config, Math.floor(Date.now() / 1000));

  let keys = await loadJwks(config);
  let key = keys.find((candidate) => candidate.kid === header.kid);
  if (!key) {
    keys = await loadJwks(config, true);
    key = keys.find((candidate) => candidate.kid === header.kid);
  }
  if (!key || !await verifyWithKey({
    encodedHeader: parts[0], encodedPayload: parts[1], signature,
  }, key)) throw accessError();
  return payload;
}

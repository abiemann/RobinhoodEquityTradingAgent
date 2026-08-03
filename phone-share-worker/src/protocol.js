export const SCHEMA_VERSION = 1;
export const SHARE_ID_RE = /^[A-Za-z0-9_-]{22,64}$/;

export const ENVELOPE_KEYS = Object.freeze([
  "captured_at",
  "ciphertext",
  "expires_at",
  "iv",
  "schema_version",
  "sequence",
  "share_id",
]);

const ISO_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const BASE64URL_RE = /^[A-Za-z0-9_-]+$/;

export class ProtocolError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ProtocolError";
    this.status = status;
    this.code = code;
  }
}

export function validateShareId(value) {
  if (typeof value !== "string" || !SHARE_ID_RE.test(value)) {
    throw new ProtocolError(400, "invalid_share_id", "Share ID is invalid.");
  }
  return value;
}

export function decodedBase64UrlLength(value, field) {
  if (typeof value !== "string" || !BASE64URL_RE.test(value) || value.length % 4 === 1) {
    throw new ProtocolError(422, `invalid_${field}`, `${field} must be unpadded base64url.`);
  }
  return Math.floor(value.length * 3 / 4);
}

function parseCanonicalTimestamp(value, field) {
  if (typeof value !== "string" || !ISO_UTC_RE.test(value)) {
    throw new ProtocolError(422, `invalid_${field}`, `${field} must be a canonical UTC timestamp.`);
  }
  const millis = Date.parse(value);
  if (!Number.isFinite(millis) || new Date(millis).toISOString() !== value) {
    throw new ProtocolError(422, `invalid_${field}`, `${field} is not a valid timestamp.`);
  }
  return millis;
}

function assertExactKeys(value, expected) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolError(422, "invalid_envelope", "Request body must be a JSON object.");
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new ProtocolError(422, "invalid_envelope", "Request body has missing or unexpected fields.");
  }
}

export function validateEnvelope(value, expectedShareId, now, limits, options = {}) {
  assertExactKeys(value, ENVELOPE_KEYS);
  validateShareId(expectedShareId);

  if (value.schema_version !== SCHEMA_VERSION) {
    throw new ProtocolError(422, "unsupported_schema", "Envelope schema version is unsupported.");
  }
  if (value.share_id !== expectedShareId) {
    throw new ProtocolError(422, "share_id_mismatch", "Envelope share ID does not match the URL.");
  }
  if (!Number.isSafeInteger(value.sequence) || value.sequence < 1) {
    throw new ProtocolError(422, "invalid_sequence", "Sequence must be a positive safe integer.");
  }

  const capturedAtMs = parseCanonicalTimestamp(value.captured_at, "captured_at");
  const expiresAtMs = parseCanonicalTimestamp(value.expires_at, "expires_at");
  const ivBytes = decodedBase64UrlLength(value.iv, "iv");
  const ciphertextBytes = decodedBase64UrlLength(value.ciphertext, "ciphertext");

  if (ivBytes !== 12) {
    throw new ProtocolError(422, "invalid_iv", "AES-GCM IV must be exactly 12 bytes.");
  }
  if (ciphertextBytes < 16 || ciphertextBytes > limits.maxCiphertextBytes) {
    throw new ProtocolError(413, "invalid_ciphertext_size", "Ciphertext size is outside the allowed range.");
  }
  if (capturedAtMs > now + limits.clockSkewMs) {
    throw new ProtocolError(422, "future_capture", "Capture timestamp is too far in the future.");
  }
  if (!options.allowStaleCapture && capturedAtMs < now - limits.maxCaptureAgeMs) {
    throw new ProtocolError(422, "stale_capture", "Capture timestamp is too old to publish.");
  }
  if (expiresAtMs <= now || expiresAtMs <= capturedAtMs) {
    throw new ProtocolError(422, "invalid_expiry", "Expiration must be after capture time and in the future.");
  }
  if (expiresAtMs - capturedAtMs > limits.maxTtlMs || expiresAtMs - now > limits.maxTtlMs) {
    throw new ProtocolError(422, "ttl_too_long", "Requested share lifetime exceeds the configured maximum.");
  }

  return {
    schemaVersion: value.schema_version,
    shareId: value.share_id,
    sequence: value.sequence,
    capturedAt: value.captured_at,
    capturedAtMs,
    expiresAt: value.expires_at,
    expiresAtMs,
    iv: value.iv,
    ciphertext: value.ciphertext,
    ciphertextBytes,
  };
}

function boundedInteger(raw, fallback, minimum, maximum, name) {
  const value = raw === undefined || raw === "" ? fallback : Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new ProtocolError(500, "invalid_worker_configuration", `${name} is invalid.`);
  }
  return value;
}

export function limitsFromEnv(env = {}) {
  return Object.freeze({
    maxBodyBytes: boundedInteger(env.MAX_BODY_BYTES, 393216, 1024, 1048576, "MAX_BODY_BYTES"),
    maxCiphertextBytes: boundedInteger(
      env.MAX_CIPHERTEXT_BYTES, 262144, 1024, 786432, "MAX_CIPHERTEXT_BYTES",
    ),
    maxTtlMs: boundedInteger(env.MAX_TTL_SECONDS, 7200, 60, 28800, "MAX_TTL_SECONDS") * 1000,
    maxCaptureAgeMs: boundedInteger(
      env.MAX_CAPTURE_AGE_SECONDS, 900, 60, 3600, "MAX_CAPTURE_AGE_SECONDS",
    ) * 1000,
    clockSkewMs: boundedInteger(env.CLOCK_SKEW_SECONDS, 120, 0, 600, "CLOCK_SKEW_SECONDS") * 1000,
    tombstoneMs: boundedInteger(env.TOMBSTONE_SECONDS, 86400, 3600, 604800, "TOMBSTONE_SECONDS") * 1000,
  });
}

export function constantTimeEqual(left, right) {
  const a = new TextEncoder().encode(typeof left === "string" ? left : "");
  const b = new TextEncoder().encode(typeof right === "string" ? right : "");
  const length = Math.max(a.length, b.length);
  let difference = a.length ^ b.length;
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}

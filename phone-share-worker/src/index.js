import { DurableObject } from "cloudflare:workers";
import {
  ProtocolError,
  constantTimeEqual,
  limitsFromEnv,
  validateEnvelope,
  validateShareId,
} from "./protocol.js";
import { isLoopbackRequest, requireCloudflareAccess } from "./access.js";
import { viewerResponse } from "./viewer.js";

const API_PATH_RE = /^\/api\/shares\/([A-Za-z0-9_-]{22,64})$/;

const BASE_HEADERS = Object.freeze({
  "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
  "Pragma": "no-cache",
  "Referrer-Policy": "no-referrer",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
});

function responseWithHeaders(response, extra = {}) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(BASE_HEADERS)) headers.set(name, value);
  for (const [name, value] of Object.entries(extra)) headers.set(name, value);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

function jsonResponse(value, status = 200, extra = {}) {
  return responseWithHeaders(new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  }), extra);
}

function emptyResponse(status, extra = {}) {
  return responseWithHeaders(new Response(null, { status }), extra);
}

function errorResponse(error) {
  if (error instanceof ProtocolError) {
    return jsonResponse({ error: error.code, message: error.message }, error.status);
  }
  console.error("phone-share worker request failed", error instanceof Error ? error.name : "unknown");
  return jsonResponse({ error: "internal_error", message: "The phone-share service could not complete the request." }, 500);
}

function bearerValue(request) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7) : "";
}

function validBearer(request, env) {
  const expected = typeof env.UPLOAD_TOKEN === "string" ? env.UPLOAD_TOKEN : "";
  return expected.length >= 32 && constantTimeEqual(bearerValue(request), expected);
}

function requireWriteAuthorization(request, env, accessIdentity) {
  const mode = env.WRITE_AUTH_MODE || "bearer-and-service-token";
  const bearerConfigured = typeof env.UPLOAD_TOKEN === "string" && env.UPLOAD_TOKEN.length >= 32;
  const serviceClientId = typeof env.ACCESS_SERVICE_CLIENT_ID === "string"
    ? env.ACCESS_SERVICE_CLIENT_ID : "";
  const serviceConfigured = serviceClientId.length >= 16;
  const serviceIdentityMatches = accessIdentity !== null && accessIdentity?.type === "app"
    && accessIdentity?.sub === "" && typeof accessIdentity?.common_name === "string"
    && constantTimeEqual(accessIdentity.common_name, serviceClientId);
  const configured = mode === "bearer"
    ? bearerConfigured && isLoopbackRequest(request)
    : mode === "bearer-and-service-token" && bearerConfigured && serviceConfigured;
  const authorized = configured && validBearer(request, env)
    && (mode === "bearer" || serviceIdentityMatches);

  if (!configured) {
    throw new ProtocolError(503, "write_auth_not_configured", "Write authentication is not configured safely.");
  }
  if (!authorized) {
    throw new ProtocolError(401, "unauthorized", "Valid uploader credentials are required.");
  }
}

async function readJsonBody(request, maximumBytes) {
  const contentType = (request.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    throw new ProtocolError(415, "unsupported_media_type", "Content-Type must be application/json.");
  }
  const encoding = (request.headers.get("Content-Encoding") || "identity").trim().toLowerCase();
  if (encoding !== "identity") {
    throw new ProtocolError(415, "unsupported_content_encoding", "Compressed request bodies are not accepted.");
  }

  const declaredLength = request.headers.get("Content-Length");
  if (declaredLength !== null) {
    if (!/^\d+$/.test(declaredLength)) {
      throw new ProtocolError(400, "invalid_content_length", "Content-Length is invalid.");
    }
    if (Number(declaredLength) > maximumBytes) {
      throw new ProtocolError(413, "body_too_large", "Request body exceeds the allowed size.");
    }
  }
  if (!request.body) {
    throw new ProtocolError(400, "empty_body", "A JSON request body is required.");
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
      if (bytesRead > maximumBytes) {
        await reader.cancel();
        throw new ProtocolError(413, "body_too_large", "Request body exceeds the allowed size.");
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
  } catch (error) {
    if (error instanceof ProtocolError) throw error;
    throw new ProtocolError(400, "invalid_utf8", "Request body is not valid UTF-8.");
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new ProtocolError(400, "invalid_json", "Request body is not valid JSON.");
  }
}

function apiPath(url) {
  if (url.search) throw new ProtocolError(400, "query_not_allowed", "Query parameters are not accepted.");
  const match = API_PATH_RE.exec(url.pathname);
  if (!match) return null;
  return validateShareId(match[1]);
}

export class ShareSession extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
    this.sql = ctx.storage.sql;
    this.mutationTail = Promise.resolve();
    this.sql.exec(`CREATE TABLE IF NOT EXISTS share_state (
      singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
      status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'revoked')),
      schema_version INTEGER NOT NULL,
      sequence INTEGER NOT NULL,
      captured_at TEXT NOT NULL,
      captured_at_ms INTEGER NOT NULL,
      expires_at TEXT NOT NULL,
      expires_at_ms INTEGER NOT NULL,
      iv TEXT,
      ciphertext TEXT,
      tombstone_until_ms INTEGER
    )`);
  }

  row() {
    return this.sql.exec("SELECT * FROM share_state WHERE singleton = 1").toArray()[0] || null;
  }

  serializedMutation(work) {
    const result = this.mutationTail.then(work, work);
    this.mutationTail = result.then(() => undefined, () => undefined);
    return result;
  }

  async expire(row, now) {
    const limits = limitsFromEnv(this.env);
    const tombstoneUntil = Math.max(now, row.expires_at_ms) + limits.tombstoneMs;
    this.sql.exec(
      "UPDATE share_state SET status = 'expired', iv = NULL, ciphertext = NULL, tombstone_until_ms = ? WHERE singleton = 1",
      tombstoneUntil,
    );
    await this.ctx.storage.setAlarm(tombstoneUntil);
  }

  async discardElapsedTombstone(row, now) {
    if (row && row.status !== "active" && row.tombstone_until_ms <= now) {
      this.sql.exec("DELETE FROM share_state WHERE singleton = 1");
      await this.ctx.storage.deleteAlarm();
      return true;
    }
    return false;
  }

  async fetch(request) {
    try {
      const url = new URL(request.url);
      const shareId = apiPath(url);
      if (!shareId) throw new ProtocolError(404, "not_found", "Resource not found.");
      if (request.method === "PUT") {
        return await this.serializedMutation(() => this.put(request, shareId));
      }
      if (request.method === "GET") return await this.get(request, shareId);
      if (request.method === "DELETE") {
        return await this.serializedMutation(() => this.delete());
      }
      return jsonResponse({ error: "method_not_allowed", message: "Method not allowed." }, 405, {
        "Allow": "GET, PUT, DELETE",
      });
    } catch (error) {
      return errorResponse(error);
    }
  }

  async put(request, shareId) {
    const limits = limitsFromEnv(this.env);
    const now = Date.now();
    const value = await readJsonBody(request, limits.maxBodyBytes);
    let existing = this.row();

    if (existing?.status === "active" && existing.expires_at_ms <= now) {
      await this.expire(existing, now);
      existing = this.row();
    }
    if (await this.discardElapsedTombstone(existing, now)) existing = null;
    if (existing && existing.status !== "active") {
      throw new ProtocolError(410, `share_${existing.status}`, "This share can no longer accept updates.");
    }
    const possibleExactDuplicate = existing !== null && value !== null
      && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 7
      && value.schema_version === existing.schema_version
      && value.sequence === existing.sequence
      && value.captured_at === existing.captured_at
      && value.expires_at === existing.expires_at
      && value.iv === existing.iv
      && value.ciphertext === existing.ciphertext;
    const envelope = validateEnvelope(value, shareId, now, limits, {
      allowStaleCapture: possibleExactDuplicate,
    });

    if (existing) {
      if (envelope.sequence === existing.sequence) {
        const exactDuplicate = envelope.schemaVersion === existing.schema_version
          && envelope.capturedAt === existing.captured_at
          && envelope.expiresAt === existing.expires_at
          && envelope.iv === existing.iv
          && envelope.ciphertext === existing.ciphertext;
        if (exactDuplicate) {
          return jsonResponse({
            accepted_sequence: envelope.sequence,
            duplicate: true,
            expires_at: envelope.expiresAt,
            status: "active",
          }, 200);
        }
        throw new ProtocolError(409, "rollback_rejected", "The current sequence cannot be replaced.");
      }
      if (envelope.sequence < existing.sequence || envelope.capturedAtMs < existing.captured_at_ms) {
        throw new ProtocolError(409, "rollback_rejected", "Sequence or capture time would roll the share backward.");
      }
      if (envelope.expiresAt !== existing.expires_at) {
        throw new ProtocolError(409, "expiry_change_rejected", "A share's fixed expiration cannot be changed.");
      }
      this.sql.exec(
        `UPDATE share_state
         SET schema_version = ?, sequence = ?, captured_at = ?, captured_at_ms = ?, iv = ?, ciphertext = ?
         WHERE singleton = 1`,
        envelope.schemaVersion, envelope.sequence, envelope.capturedAt, envelope.capturedAtMs,
        envelope.iv, envelope.ciphertext,
      );
    } else {
      this.sql.exec(
        `INSERT INTO share_state
         (singleton, status, schema_version, sequence, captured_at, captured_at_ms,
          expires_at, expires_at_ms, iv, ciphertext, tombstone_until_ms)
         VALUES (1, 'active', ?, ?, ?, ?, ?, ?, ?, ?, NULL)`,
        envelope.schemaVersion, envelope.sequence, envelope.capturedAt, envelope.capturedAtMs,
        envelope.expiresAt, envelope.expiresAtMs, envelope.iv, envelope.ciphertext,
      );
    }
    await this.ctx.storage.setAlarm(envelope.expiresAtMs);
    return jsonResponse({
      accepted_sequence: envelope.sequence,
      expires_at: envelope.expiresAt,
      status: "active",
    }, existing ? 200 : 201);
  }

  async get(request, shareId) {
    const now = Date.now();
    let row = this.row();
    if (!row) throw new ProtocolError(404, "share_not_found", "Share was not found.");
    if (row.status === "active" && row.expires_at_ms <= now) {
      await this.expire(row, now);
      row = this.row();
    }
    if (await this.discardElapsedTombstone(row, now)) {
      throw new ProtocolError(404, "share_not_found", "Share was not found.");
    }
    if (row.status !== "active") {
      throw new ProtocolError(410, `share_${row.status}`, `Share is ${row.status}.`);
    }

    const etag = `"rhmra-${row.sequence}"`;
    if (request.headers.get("If-None-Match") === etag) {
      return emptyResponse(304, { "ETag": etag });
    }
    return jsonResponse({
      schema_version: row.schema_version,
      share_id: shareId,
      sequence: row.sequence,
      captured_at: row.captured_at,
      expires_at: row.expires_at,
      iv: row.iv,
      ciphertext: row.ciphertext,
    }, 200, {
      "ETag": etag,
      "X-RHMRA-Sequence": String(row.sequence),
      "X-RHMRA-Expires-At": row.expires_at,
    });
  }

  async delete() {
    const now = Date.now();
    const limits = limitsFromEnv(this.env);
    const row = this.row();
    if (!row) return emptyResponse(204);
    if (row.status === "active") {
      const tombstoneUntil = now + limits.tombstoneMs;
      this.sql.exec(
        "UPDATE share_state SET status = 'revoked', iv = NULL, ciphertext = NULL, tombstone_until_ms = ? WHERE singleton = 1",
        tombstoneUntil,
      );
      await this.ctx.storage.setAlarm(tombstoneUntil);
    }
    return emptyResponse(204);
  }

  async alarm() {
    return this.serializedMutation(() => this.handleAlarm());
  }

  async handleAlarm() {
    const now = Date.now();
    const row = this.row();
    if (!row) return;
    if (row.status === "active") {
      if (row.expires_at_ms > now) {
        await this.ctx.storage.setAlarm(row.expires_at_ms);
      } else {
        await this.expire(row, now);
      }
      return;
    }
    if (row.tombstone_until_ms > now) {
      await this.ctx.storage.setAlarm(row.tombstone_until_ms);
    } else {
      this.sql.exec("DELETE FROM share_state WHERE singleton = 1");
      await this.ctx.storage.deleteAlarm();
    }
  }
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/view")) {
        if (url.pathname === "/") {
          return responseWithHeaders(new Response(null, { status: 302, headers: { Location: "/view" } }));
        }
        return viewerResponse();
      }
      if (request.method === "GET" && url.pathname === "/api/auth" && !url.search) {
        await requireCloudflareAccess(request, env);
        return responseWithHeaders(new Response(null, { status: 303, headers: { Location: "/view" } }));
      }
      if (request.method === "POST" && url.pathname === "/api/preflight" && !url.search) {
        const accessIdentity = await requireCloudflareAccess(request, env);
        requireWriteAuthorization(request, env, accessIdentity);
        if (!env.SHARE_SESSION) {
          throw new ProtocolError(503, "storage_not_configured", "Durable Object storage is not configured.");
        }
        return emptyResponse(204);
      }

      const shareId = apiPath(url);
      if (!shareId) return jsonResponse({ error: "not_found", message: "Resource not found." }, 404);
      const accessIdentity = await requireCloudflareAccess(request, env);
      if (!["GET", "PUT", "DELETE"].includes(request.method)) {
        return jsonResponse({ error: "method_not_allowed", message: "Method not allowed." }, 405, {
          "Allow": "GET, PUT, DELETE",
        });
      }
      if (request.method === "PUT" || request.method === "DELETE") {
        requireWriteAuthorization(request, env, accessIdentity);
      }
      if (!env.SHARE_SESSION) {
        throw new ProtocolError(503, "storage_not_configured", "Durable Object storage is not configured.");
      }
      const objectId = env.SHARE_SESSION.idFromName(shareId);
      return await env.SHARE_SESSION.get(objectId).fetch(request);
    } catch (error) {
      return errorResponse(error);
    }
  },
};

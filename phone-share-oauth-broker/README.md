# RHMRA Google OAuth broker

This directory contains the small, stateless Cloudflare Worker used by released RHMRA desktop agents to exchange Google OAuth authorization codes and refresh tokens. It exists because Google's current Desktop OAuth client requires its client secret during token exchange, while distributing that secret in source code or an installer would expose it.

End users do **not** deploy this Worker, create a credential file, or configure an environment variable. This is a one-time maintainer deployment for the public Agent release.

## What it does—and does not do

The Worker exposes one endpoint:

```text
POST /oauth/token
Content-Type: application/x-www-form-urlencoded
```

It accepts only the two exchanges the desktop Agent needs:

- `authorization_code` with the exact Google client ID, an S256 PKCE verifier, and an exact `http://127.0.0.1:<port>/oauth2/callback` redirect;
- `refresh_token` with the exact Google client ID.

The Worker adds the Google Desktop client secret from an encrypted Worker secret and relays the request to the fixed `https://oauth2.googleapis.com/token` endpoint. Responses are schema-checked and reduced to the OAuth fields the Agent needs.

The Worker owns no database, Durable Object, KV namespace, analytics payload, cookies, or CORS access. Cloudflare's ephemeral, point-of-presence rate limiter counts requests for up to one minute using a key derived from the connecting IP address; this is used only for abuse prevention. The Worker code does not log or retain that address, authorization codes, access tokens, refresh tokens, Drive data, dashboard snapshots, brokerage credentials, or pairing keys. Dashboard snapshots continue to travel directly between the Agent and the user's own hidden Google Drive app-data folder.

## Cost and operating profile

This implementation uses only ordinary stateless Worker requests—no paid storage or other Cloudflare products. A device normally makes one token exchange when connected and an occasional refresh exchange afterward, so normal RHMRA usage is far below the Workers Free plan request allowance. Confirm the current terms before deployment in Cloudflare's official [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/) and [platform limits](https://developers.cloudflare.com/workers/platform/limits/) documentation.

Cloudflare can change its plans. Set a usage notification in the Cloudflare dashboard if you want an additional operational guardrail.

## One-time maintainer deployment

Prerequisites:

- Node.js 20 or newer;
- a Cloudflare account with Workers enabled;
- multi-factor authentication enabled on the maintainer's Cloudflare account;
- the existing RHMRA Google Desktop OAuth client's secret.

From this directory:

```powershell
corepack enable
pnpm install --frozen-lockfile
pnpm test
pnpm exec wrangler login --use-keyring --scopes account:read user:read workers_scripts:write
pnpm run deploy
pnpm exec wrangler secret put GOOGLE_DESKTOP_CLIENT_SECRET
```

When Wrangler asks for the secret, paste it only into that prompt. Do not put it in `wrangler.jsonc`, source control, an issue, a chat, or a build log. Cloudflare stores Worker secrets encrypted.

The initial deploy creates the Worker, and `secret put` then attaches the encrypted secret to that deployment immediately; a second deploy is not required. Until the secret is attached, the Worker fails closed with a generic unavailable response.

On a one-time deployment workstation, you can remove Wrangler's local login after the secret is attached:

```powershell
pnpm exec wrangler logout
```

Logging out is optional; you can sign in again later to deploy an update or rotate the secret.

The public client ID is deliberately pinned in `wrangler.jsonc`; OAuth client IDs are identifiers, not credentials. The production secret must correspond to that exact client ID.

After deployment, record the exact endpoint:

```text
https://rhmra-google-oauth-broker.<your-workers-subdomain>.workers.dev/oauth/token
```

Pin that HTTPS URL in the released Agent configuration. Do not use a preview URL, add a query string, or put Cloudflare Access in front of this endpoint: installed desktop clients cannot safely carry a shared Access credential.

## Local verification

Unit tests use a fake Google endpoint and never need the real client secret:

```powershell
pnpm test
```

For an optional local Worker run, copy `.dev.vars.example` to `.dev.vars`, replace the placeholder locally, and run `pnpm run dev`. `.dev.vars` is ignored by Git. Avoid testing with a live OAuth code unless necessary; codes are short-lived, single-use credentials.

## Security behavior

- Only exact `POST /oauth/token` requests are accepted; query strings and browser CORS access are rejected.
- Request media type, encoding, size, fields, duplicates, token shapes, PKCE verifier, client ID, and loopback redirect are validated before Google is called.
- A caller-supplied `client_secret` is always rejected.
- Requests are limited to 30 per minute per Cloudflare client-address key before Google is called; a missing or unavailable limiter fails closed.
- Outbound traffic is pinned to Google's token endpoint, redirects are disabled, and requests have a 10-second timeout.
- Google responses have a size limit, must be JSON, and are allow-listed before returning.
- Every response uses `Cache-Control: no-store` and related security headers.
- Worker invocation logs, Logpush, and observability are explicitly disabled in the deployment configuration.
- Exceptions are returned as generic errors; request bodies and secrets are never written to logs.

The endpoint is intentionally usable by installed clients without another shared secret. Its protection is the same OAuth proof Google requires: a valid one-time authorization code plus PKCE verifier, or a valid refresh token, all bound to the pinned client. Invalid traffic cannot mint tokens.

## Rotation and rollback

To rotate the Google client secret without changing source:

```powershell
pnpm exec wrangler secret put GOOGLE_DESKTOP_CLIENT_SECRET
```

The rotated secret becomes active immediately; no code deploy is needed. Then complete a fresh authorization and a refresh-token test. The Agent retains its external Desktop credential-file override as a developer-only rollback path; ordinary users should never need it.

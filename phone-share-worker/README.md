# RHMRA phone-share Worker

This optional Cloudflare Worker lets the local RHMRA Dashboard publish a temporary, read-only view for a phone. The laptop makes outbound HTTPS requests only: it does not open a port, accept internet traffic, or expose the Robinhood connector.

For normal personal use, this is designed to fit within Cloudflare's free allowances. Cloudflare controls its plans and may ask for payment details when Zero Trust is enabled, so confirm the current terms during signup. Every RHMRA user deploys this Worker into their own Cloudflare account; the project owner does not operate a shared service.

## What is protected

- The browser encrypts an allowlisted dashboard snapshot with AES-256-GCM before upload.
- The Worker stores only ciphertext, its IV, and the minimum sequence/expiry metadata needed to operate the share. It never receives the decryption key.
- The key travels in the QR URL fragment. URL fragments are not sent in HTTP requests.
- `/view` is a fixed public bootstrap page with no user data. It saves the fragment only in `sessionStorage`, removes it from browser history, and sends the browser through Cloudflare Access at `/api/auth`.
- Every `/api/*` request requires a Cloudflare Access JWT. The Worker verifies the JWT's RS256 signature, issuer, audience, and lifetime itself.
- Uploads and revocations additionally require both a private uploader bearer token and the configured Cloudflare service-token identity.
- Preview URLs are disabled. Runtime code has no third-party scripts, fonts, analytics, QR services, or other browser dependencies.
- A share lasts two hours by default and can never exceed eight hours. Revocation or expiration immediately clears its ciphertext; a small `410 Gone` tombstone remains for one day to prevent replay, then it is removed.

The QR link is still sensitive: anyone who has the link has the decryption key. Cloudflare Access, short expiry, and immediate revocation are independent protections if the QR or link is accidentally shared.

## One-time Cloudflare setup

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/abiemann/RobinhoodEquityTradingAgent/tree/main/phone-share-worker)

1. Sign in to a free Cloudflare account and open the deployment link above. If Cloudflare asks you to connect GitHub, choose **New GitHub connection**. On GitHub's **Install & Authorize Cloudflare Workers and Pages** page:

   - choose **Only select repositories** rather than **All repositories**;
   - follow GitHub's repository prompt; and
   - click **Install & Authorize**.

   You do not need to fork RHMRA first. Cloudflare reads the public source during setup, creates a separate deployment repository in your GitHub account, and automatically receives access to that new repository. Only the self-contained `phone-share-worker/` directory is copied, built, and deployed; the trading application and its files are not deployed.
2. After GitHub returns you to Cloudflare, finish the application form:

   - keep the project name `rhmra-phone-share`, or use another descriptive name;
   - select **Create private Git repository** if you want Cloudflare's generated deployment repository to be private;
   - confirm that `WRITE_AUTH_MODE` appears exactly once as `bearer-and-service-token` and `VERIFY_ACCESS_JWT` appears exactly once as `true`;
   - leave **Build command** blank and **Deploy command** set to `npm run deploy`; and
   - confirm that `UPLOAD_TOKEN` is a separate random value of at least 32 characters, then save it securely because the local dashboard uploader needs that same value.

   Only `UPLOAD_TOKEN` should be masked. If `WRITE_AUTH_MODE` or `VERIFY_ACCESS_JWT` also appears as a masked duplicate, cancel the deployment and start again from the latest RHMRA revision. **Builds for non-production branches** may remain selected; it only controls preview builds. Click **Deploy**. After deployment, open GitHub **Settings → Applications → Cloudflare Workers and Pages**, keep **Only select repositories**, and leave access only to the new Cloudflare deployment repository.
3. Note the production URL, such as `https://rhmra-phone-share.your-name.workers.dev`.
4. In **Zero Trust → Access controls → Service credentials → Service Tokens**, create a token named `RHMRA dashboard uploader`. Save both its Client ID and Client Secret; Cloudflare displays the secret only once.
5. In **Zero Trust → Access controls → Applications**, create one self-hosted application for exactly:

   `rhmra-phone-share.your-name.workers.dev/api/*`

   Leave `/view` outside Access. This is intentional: it safely preserves the QR fragment before an authentication redirect.
6. Add two Access policies to that application:

   - an **Allow** policy containing only the email address that may view the phone dashboard; and
   - a **Service Auth** policy containing only the `RHMRA dashboard uploader` service token.

   Use Cloudflare's built-in identity provider or enable one-time PIN email login. Do not add a `Bypass` policy.
7. From the application's settings, copy its **Application Audience (AUD) Tag**. Also note the Zero Trust team domain, such as `https://your-team.cloudflareaccess.com`.
8. In the Worker's **Settings → Variables and Secrets**, configure:

   | Name | Kind | Value |
   |---|---|---|
   | `ACCESS_TEAM_DOMAIN` | encrypted secret or variable | `https://your-team.cloudflareaccess.com` |
   | `ACCESS_AUD` | encrypted secret or variable | the Access application's AUD tag |
   | `ACCESS_SERVICE_CLIENT_ID` | encrypted secret or variable | the uploader service token's Client ID |
   | `UPLOAD_TOKEN` | encrypted secret | a separate random value of at least 32 characters |

   `UPLOAD_TOKEN` is not the Cloudflare Client Secret. Use the separate value created on the deployment form. If you still need to generate it, use one of these cryptographically secure commands, copy the output into the Worker secret, and keep it available for the local setup in the next step:

   PowerShell:

   ```powershell
   py -3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Bash:

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```
9. Restart the local dashboard after setting these environment variables:

   | Name | Value |
   |---|---|
   | `RHMRA_PHONE_SHARE_URL` | the fixed HTTPS Worker base URL |
   | `RHMRA_PHONE_SHARE_UPLOAD_TOKEN` | the same value as Worker `UPLOAD_TOKEN` |
   | `RHMRA_PHONE_SHARE_CF_CLIENT_ID` | the Access service token Client ID |
   | `RHMRA_PHONE_SHARE_CF_CLIENT_SECRET` | the Access service token Client Secret |

   Optional local settings are `RHMRA_PHONE_SHARE_VIEWER_URL` (defaults to `<base URL>/view` and, if set, must be that same origin's exact `/view` path) and `RHMRA_PHONE_SHARE_TTL_SECONDS` (defaults to `7200`; accepted range `300`–`28800`). The Worker also defaults `MAX_TTL_SECONDS` to `7200`; if you intentionally choose a longer local lifetime, raise that Worker variable to the same value, never above `28800`. Never put any of these credentials in `constants.md`, Git, a task prompt, or a QR code.

   The following examples set values only for the current terminal process and then start the dashboard. They do not create a configuration file.

   PowerShell:

   ```powershell
   $env:RHMRA_PHONE_SHARE_URL = Read-Host 'Worker URL (https://...workers.dev)'
   $env:RHMRA_PHONE_SHARE_UPLOAD_TOKEN = Read-Host 'Paste UPLOAD_TOKEN'
   $env:RHMRA_PHONE_SHARE_CF_CLIENT_ID = Read-Host 'Paste Access service-token Client ID'
   $env:RHMRA_PHONE_SHARE_CF_CLIENT_SECRET = Read-Host 'Paste Access service-token Client Secret'
   py -3 dashboard/serve.py
   ```

   Bash:

   ```bash
   read -r -p 'Worker URL (https://...workers.dev): ' RHMRA_PHONE_SHARE_URL
   read -r -p 'Access service-token Client ID: ' RHMRA_PHONE_SHARE_CF_CLIENT_ID
   read -r -s -p 'UPLOAD_TOKEN: ' RHMRA_PHONE_SHARE_UPLOAD_TOKEN; echo
   read -r -s -p 'Access service-token Client Secret: ' RHMRA_PHONE_SHARE_CF_CLIENT_SECRET; echo
   export RHMRA_PHONE_SHARE_URL RHMRA_PHONE_SHARE_UPLOAD_TOKEN
   export RHMRA_PHONE_SHARE_CF_CLIENT_ID RHMRA_PHONE_SHARE_CF_CLIENT_SECRET
   python3 dashboard/serve.py
   ```

   Close that terminal when finished to remove its process-local environment. If you later choose persistent environment storage, use your operating system's secret storage and never a tracked repository file.

After setup, **View on Phone** generates the encrypted session and QR code. Scanning it uses the phone's normal browser—no phone app is required. **Stop sharing** revokes the remote snapshot immediately.

## Manual development and deployment

Requires Node.js 20 or newer:

```text
cd phone-share-worker
npm install
npm test
npx wrangler deploy
```

For command-line configuration, each Worker value can be entered without writing it to disk:

```text
npx wrangler secret put ACCESS_TEAM_DOMAIN
npx wrangler secret put ACCESS_AUD
npx wrangler secret put ACCESS_SERVICE_CLIENT_ID
npx wrangler secret put UPLOAD_TOKEN
```

For local-only Worker development, copy `local-dev.vars.example` to `.dev.vars`. The local example explicitly disables Access JWT verification so `wrangler dev` can run without Cloudflare Access. The separate `.dev.vars.example` file intentionally declares only the production `UPLOAD_TOKEN` prompt used by Deploy to Cloudflare. `.dev.vars` is ignored by Git. Local overrides work only when the request hostname is `localhost`, a `*.localhost` name, `127.0.0.1`, or `::1`; the Worker returns a configuration failure if either override is accidentally deployed. The checked-in production configuration always requires both Access and the service-token identity.

## Wire contract

The uploader calls `PUT /api/shares/<share_id>` with this exact JSON envelope:

```json
{
  "schema_version": 1,
  "share_id": "random-base64url-id",
  "sequence": 1,
  "captured_at": "2026-08-03T18:00:00.000Z",
  "expires_at": "2026-08-03T20:00:00.000Z",
  "iv": "base64url-12-byte-AES-GCM-IV",
  "ciphertext": "base64url-ciphertext-and-tag"
}
```

AES-GCM additional authenticated data is the UTF-8 encoding of:

```text
JSON.stringify([schema_version, share_id, sequence, captured_at, expires_at])
```

Sequences and capture times can only move forward, and expiry is fixed when a session is created. Retrying the exact current envelope is idempotent; reusing its sequence with different content is rejected. Request bodies, ciphertext, IDs, timestamps, and JSON keys all have strict limits. `GET /api/shares/<id>` returns the envelope, while authenticated `DELETE /api/shares/<id>` revokes it.

The viewer checks sequence, capture time, fixed expiry, IV plus ciphertext identity, decrypted schema, and snapshot freshness before replacing its current display. Its decrypted allowlist contains only dashboard mode, account totals, positions, summarized run chips, and rules-era totals—never account numbers, order IDs, raw ledgers, gate files, reports, credentials, or local filesystem paths.

# RHMRA phone-share Worker

This optional Cloudflare Worker lets the local RHMRA Dashboard publish a temporary, read-only view for a phone. The laptop makes outbound HTTPS requests only: it does not open a port, accept internet traffic, or expose the Robinhood connector.

For normal personal use, this is designed to fit within Cloudflare's free allowances. Cloudflare currently requires payment details to activate Zero Trust Free, but states that the free plan is not charged; confirm that the order summary says `$0/month` before continuing. Every RHMRA user deploys this Worker into their own Cloudflare account; the project owner does not operate a shared service.

## What is protected

- The browser encrypts an allowlisted dashboard snapshot with AES-256-GCM before upload.
- The Worker stores only ciphertext, its IV, and the minimum sequence/expiry metadata needed to operate the share. It never receives the decryption key.
- The key travels in the QR URL fragment. URL fragments are not sent in HTTP requests.
- `/view` is a fixed public bootstrap page with no user data. It saves the fragment only in `sessionStorage`, removes it from browser history, and sends the browser through Cloudflare Access at `/api/auth`.
- Every `/api/*` request requires a Cloudflare Access JWT. The Worker verifies the JWT's RS256 signature, issuer, audience, and lifetime itself.
- Uploads and revocations additionally require both a private uploader bearer token and the configured Cloudflare service-token identity.
- Preview URLs are disabled. Runtime code has no third-party scripts, fonts, analytics, QR services, or other browser dependencies.
- A share lasts two hours by default and can never exceed eight hours. A successful revocation, or automatic expiration, clears its ciphertext; a small `410 Gone` tombstone remains for one day to prevent replay, then it is removed.

The QR link is still sensitive: anyone who has the link has the decryption key. Cloudflare Access, short expiry, and revocation are independent protections if the QR or link is accidentally shared. If a revocation request fails, the encrypted snapshot remains only until its original expiry.

## Before you begin

Allow about 30–45 minutes the first time. Cloudflare changes labels occasionally, but the values and checkpoints below remain the same. Complete each checkpoint before moving on.

You need:

- a Cloudflare account;
- a GitHub account for Cloudflare's generated deployment repository;
- an email account you can open on the phone; and
- the current RHMRA project on the computer that runs the dashboard.

You do **not** need to buy a domain, install a phone app, open a laptop port, configure a tunnel, fork RHMRA, or run anything as Administrator. Phone sharing is optional and remains off until you complete this guide and click **Create secure QR code**.

### Private credential worksheet

Save these values in a password manager or other secure private note while you work. Cloudflare displays the service-token Client Secret only once.

| Value to record | Worker setting | Local dashboard setting or use |
|---|---|---|
| Production Worker base URL, such as `https://rhmra-phone-share.your-name.workers.dev` | — | `RHMRA_PHONE_SHARE_URL` |
| New random upload token, at least 32 characters | `UPLOAD_TOKEN` | `RHMRA_PHONE_SHARE_UPLOAD_TOKEN` — the value must be identical |
| Access service-token Client ID | `ACCESS_SERVICE_CLIENT_ID` | `RHMRA_PHONE_SHARE_CF_CLIENT_ID` |
| Access service-token Client Secret | **Never store in the Worker** | `RHMRA_PHONE_SHARE_CF_CLIENT_SECRET` |
| Access application's Audience (AUD) tag | `ACCESS_AUD` | — |
| Full team URL, such as `https://your-team.cloudflareaccess.com` | `ACCESS_TEAM_DOMAIN` | — |
| Email allowed to view the dashboard | Access **Allow** policy | — |

`UPLOAD_TOKEN` is a separate random secret. It is **not** the AUD, Client ID, or Client Secret. There is no Worker variable named `RHMRA_PHONE_SHARE_UPLOAD_TOKEN`; that longer name is used only on the computer running RHMRA.

The decryption key is different again: the browser generates it when a share starts and puts it only in the QR/private-link fragment. Never put any credential or QR screenshot in `constants.md`, Git, a task prompt, a report, a command-line argument, or a public screenshot.

### How close can setup get to one click?

The **Deploy to Cloudflare** button already acts as the deployment package: it copies the Worker, creates its deployment repository, configures the Durable Object, and builds the service. The first setup cannot safely be completely one click because GitHub authorization, activation of the $0 Zero Trust plan, the allowed viewer email, Access policies, and one-time secret capture all require the account owner to make or approve a security decision.

A future Windows guided launcher can still make this much easier. A safe launcher would generate and validate values, open the exact Cloudflare pages, run the supplied preflight, store local credentials with Windows-protected secret storage, and start the dashboard. The repeat launch could then be one click. It must not put secrets in a plaintext `.bat` file, repository file, command-line argument, or log. Fully automating Cloudflare would require a powerful API token and is not the recommended beginner tradeoff.

## One-time Cloudflare setup

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/abiemann/RobinhoodEquityTradingAgent/tree/main/phone-share-worker)

### Step 1 — Generate the upload token

Generate one random value now and copy it into your private worksheet. You will paste this **same value** into Cloudflare as `UPLOAD_TOKEN` and later on the RHMRA computer as `RHMRA_PHONE_SHARE_UPLOAD_TOKEN`.

PowerShell:

```powershell
py -3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Bash:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Checkpoint: the output is a random-looking string of about 43 characters. Do not reuse another Cloudflare credential.

### Step 2 — Connect Cloudflare to GitHub

1. Click **Deploy to Cloudflare** above and sign in to Cloudflare.
2. If prompted to connect GitHub, click **New GitHub connection**.
3. GitHub opens **Install & Authorize Cloudflare Workers and Pages**. Select **Only select repositories**; do not grant access to all repositories.
4. If GitHub requires one repository before the new deployment repository exists, select a harmless empty repository you own. A new GitHub account can first create an empty private repository named `cloudflare-bootstrap`. Do not select a repository containing credentials or private financial work.
5. Click **Install & Authorize**. Cloudflare can read the public RHMRA source without a fork and creates a new deployment repository in your account.

Cloudflare creates a separate deployment repository in your GitHub account; no RHMRA fork is required. Only this self-contained Worker is copied and deployed—none of the trading application is uploaded.

### Step 3 — Deploy the Worker

Back in Cloudflare, complete **Set up your application**:

| Field | Value |
|---|---|
| Git account | your GitHub account |
| Create private Git repository | recommended |
| Project name | `rhmra-phone-share` (use this exact name for the beginner path) |
| `UPLOAD_TOKEN` | the random token from Step 1 |
| `WRITE_AUTH_MODE` | `bearer-and-service-token` |
| `VERIFY_ACCESS_JWT` | `true` |
| Build command | leave blank |
| Deploy command | `npm run deploy` |

Keep the supplied `MAX_*`, clock-skew, and tombstone values unchanged. **Builds for non-production branches** may remain selected.

`WRITE_AUTH_MODE` and `VERIFY_ACCESS_JWT` must each appear exactly once as visible text. `UPLOAD_TOKEN` should be masked. If either of the first two appears again as a masked duplicate, cancel and restart from the current RHMRA Deploy link.

Click **Deploy** and wait for **Success! Build completed**. Record the production URL, for example `https://rhmra-phone-share.your-name.workers.dev`. Save only the base URL—do not append `/view`, `/api`, a query string, or another path.

Check these results before continuing:

- Worker **Bindings** lists the Durable Object binding `SHARE_SESSION`.
- Opening `<Worker URL>/view` displays the fixed RHMRA phone viewer shell. It contains no financial data yet.
- In GitHub **Settings → Applications → Cloudflare Workers and Pages**, change repository access to only the new Cloudflare deployment repository. Remove the temporary `cloudflare-bootstrap` selection if you used it.

### Step 4 — Activate Zero Trust Free

Skip this step only if your Cloudflare account already has Zero Trust enabled.

1. Open Cloudflare **Zero Trust** or **Cloudflare One**.
2. Select **Zero Trust Free**—not Standard, Enterprise, or the paid Workers plan.
3. Choose a team name if prompted.
4. Cloudflare currently requires payment details for Zero Trust Free. Confirm the order summary says **Zero Trust Free**, `$0/month`, and `$0 due today`, then activate it.
5. Click **Continue to Zero Trust**. You should arrive at the Cloudflare One dashboard.
6. On **Cloudflare One → Overview**, find **Account details → Team name**; if it is not shown there, open **Cloudflare One → Settings**. Turn that name into the exact team URL. For example, team name `quiet-river-1234` becomes `https://quiet-river-1234.cloudflareaccess.com`.
7. Verify the value before saving it: open `<team URL>/cdn-cgi/access/certs` in a browser. It should return a JSON document containing `"keys"`. This endpoint publishes signing keys and contains no private credential. Do not continue if it shows an error page.

### Step 5 — Create the uploader service token

1. Go to **Zero Trust → Access controls → Service credentials → Service Tokens**.
2. Click **Create Service Token** or **Add a service token**.
3. Name it `RHMRA dashboard uploader`.
4. Choose its lifetime. One year is easier to rotate regularly; two years is also valid if you prefer fewer renewals. Set a calendar reminder before it expires.
5. Click **Generate token**.
6. Immediately copy both the **Client ID** and **Client Secret** into your private worksheet. Cloudflare shows the Client Secret only once; it cannot be recovered later.

Checkpoint: you have two different service-token values. Do not use either one as `UPLOAD_TOKEN`.

### Step 6 — Create the Access application and viewer policy

1. Go to **Zero Trust → Access controls → Applications**.
2. Click **Add an application** or **Create new application**.
3. Choose **Self-hosted and private**. Select **Public DNS**, then use **Switch to custom input** if Cloudflare shows separate subdomain/domain boxes.
4. Enter exactly your Worker hostname followed by `/api/*`, without `https://`. Example:

   `rhmra-phone-share.your-name.workers.dev/api/*`

   Do **not** protect `/view`; that public page is a fixed, data-free bootstrap that preserves the QR fragment during the Access redirect. Leave browser-based RDP/SSH/VNC rendering off.
5. Click **Create new policy** and enter:

   | Field | Value |
   |---|---|
   | Policy name | `RHMRA phone viewer` |
   | Action | **Allow** |
   | Include rule | **Emails** |
   | Email | the exact address you will use on the phone; using your Cloudflare-account email is simplest |

6. Save the policy. Under **Authentication**, keep Cloudflare's automatically configured identity provider available. The Cloudflare-account email can use its normal Cloudflare sign-in. A different email in the **Allow** policy can use Cloudflare's emailed one-time PIN without a separate identity-provider setup. If the page shows no available login method, open **Cloudflare One → Settings → Authentication → Login methods** and confirm the Cloudflare identity provider is enabled. **Apply instant authentication** is optional when only one login method is available.

### Step 7 — Add the uploader policy and finish the application

1. Add a second policy with:

   | Field | Value |
   |---|---|
   | Policy name | `RHMRA dashboard uploader` |
   | Action | **Service Auth** |
   | Include rule | **Service Token** |
   | Token | `RHMRA dashboard uploader` from Step 5 |

2. Save that policy.
3. In the application's **Details** section, set **Name** to `RHMRA phone dashboard API`.
4. Click the application's final **Create** button. Saving both policies without creating the application is not enough.

Checkpoint: the Applications list shows one application for `<Worker hostname>/api/*` with two policies—one **Allow** and one **Service Auth**. There must be no **Bypass** policy. The first phone visit showing a Cloudflare login or emailed one-time PIN is expected.

Open **Access controls → Applications → RHMRA phone dashboard API → Additional settings**, then copy **Application Audience (AUD) Tag** into your worksheet. Also confirm the full team URL from Step 4: `https://your-team.cloudflareaccess.com`, with no extra path.

### Step 8 — Add the four Worker secrets and deploy them

Open **Workers & Pages → your Worker → Settings → Variables and secrets**. Add or rotate these four values. Using **Secret** for all four is recommended.

| Worker name | Paste this value |
|---|---|
| `ACCESS_TEAM_DOMAIN` | full `https://your-team.cloudflareaccess.com` URL |
| `ACCESS_AUD` | Access application's AUD tag |
| `ACCESS_SERVICE_CLIENT_ID` | service-token Client ID—not its Client Secret |
| `UPLOAD_TOKEN` | random token from Step 1 |

Do not add the service-token Client Secret to the Worker. It belongs only in local `RHMRA_PHONE_SHARE_CF_CLIENT_SECRET`.

Leave the existing plaintext settings intact: `WRITE_AUTH_MODE=bearer-and-service-token`, `VERIFY_ACCESS_JWT=true`, and the supplied `MAX_*`, clock-skew, and tombstone values.

Click **Deploy** after adding or rotating the secrets, and wait for a successful new Worker version. Merely entering a value without deploying it does not activate the change.

Cloudflare may show an orange message asking you to update the Wrangler configuration. That reminder is informational for dashboard-managed secrets. **Do not copy secret values into `wrangler.jsonc` or Git.** The tracked configuration contains only non-secret defaults. The current deployment also includes the required `global_fetch_strictly_public` compatibility flag automatically.

Checkpoint: **Variables and secrets** lists all four names as encrypted, the latest deployment succeeded, and **Bindings** still lists `SHARE_SESSION`.

### Step 9 — Check Cloudflare before configuring RHMRA

Do not continue until these checks pass:

1. Open `<Worker URL>/view` in a private browser window. The fixed viewer shell should load without showing account data.
2. Open `<Worker URL>/api/auth`. Cloudflare should ask you to sign in. Use the exact email in the viewer **Allow** policy.
3. After successful login or one-time PIN entry, the browser should return to `/view`.
4. Recheck the Applications list: destination `/api/*`, one **Allow** policy, one **Service Auth** policy, and no **Bypass** policy.
5. Recheck the Worker: four encrypted settings, successful latest deployment, and the `SHARE_SESSION` binding.

These browser checks validate the viewer path and email login. The first 5-minute share in Step 11 validates the independent uploader credentials and encrypted storage end to end.

### Step 10 — Configure and start the local dashboard

Set these four values in the **same terminal** that starts `dashboard/serve.py`:

| Name | Value |
|---|---|
| `RHMRA_PHONE_SHARE_URL` | fixed HTTPS Worker base URL, with no path |
| `RHMRA_PHONE_SHARE_UPLOAD_TOKEN` | exactly the same value as Worker `UPLOAD_TOKEN` |
| `RHMRA_PHONE_SHARE_CF_CLIENT_ID` | Access service-token Client ID |
| `RHMRA_PHONE_SHARE_CF_CLIENT_SECRET` | Access service-token Client Secret |

The base URL must use HTTPS and contain no path, query, or fragment. The upload token must be at least 32 characters, Client ID at least 16, and Client Secret at least 32. If any value is missing or malformed, the feature stays off and makes no remote request.

Optional settings are `RHMRA_PHONE_SHARE_VIEWER_URL` (normally omit it; the default is `<base URL>/view`) and `RHMRA_PHONE_SHARE_TTL_SECONDS` (default `7200`; accepted range `300`–`28800`). The Worker defaults `MAX_TTL_SECONDS` to `7200`; if you deliberately allow a longer share, change both values to the same number and never exceed `28800`.

The examples below keep values only in this terminal process; opening a new terminal loses them. Stop any already-running dashboard, enter the settings, and start the server from that same terminal. Changing any value requires a restart.

PowerShell hides both secret values while you paste them:

```powershell
$env:RHMRA_PHONE_SHARE_URL = Read-Host 'Worker base URL (https://...workers.dev)'
$upload = Read-Host 'Paste UPLOAD_TOKEN' -AsSecureString
$env:RHMRA_PHONE_SHARE_UPLOAD_TOKEN = [Net.NetworkCredential]::new('', $upload).Password
$env:RHMRA_PHONE_SHARE_CF_CLIENT_ID = Read-Host 'Paste Access service-token Client ID'
$clientSecret = Read-Host 'Paste Access service-token Client Secret' -AsSecureString
$env:RHMRA_PHONE_SHARE_CF_CLIENT_SECRET = [Net.NetworkCredential]::new('', $clientSecret).Password
py -3 dashboard/phone_share_preflight.py
py -3 dashboard/serve.py
```

Bash:

```bash
read -r -p 'Worker base URL (https://...workers.dev): ' RHMRA_PHONE_SHARE_URL
read -r -p 'Access service-token Client ID: ' RHMRA_PHONE_SHARE_CF_CLIENT_ID
read -r -s -p 'UPLOAD_TOKEN: ' RHMRA_PHONE_SHARE_UPLOAD_TOKEN; echo
read -r -s -p 'Access service-token Client Secret: ' RHMRA_PHONE_SHARE_CF_CLIENT_SECRET; echo
export RHMRA_PHONE_SHARE_URL RHMRA_PHONE_SHARE_UPLOAD_TOKEN
export RHMRA_PHONE_SHARE_CF_CLIENT_ID RHMRA_PHONE_SHARE_CF_CLIENT_SECRET
python3 dashboard/phone_share_preflight.py
python3 dashboard/serve.py
```

Do not start the server unless the command prints **Phone-share uploader preflight passed**. The preflight calls a dedicated data-free endpoint that proves Cloudflare Access, the upload token, the service token, Worker configuration, and Durable Object binding are ready. It creates no share or storage object and never prints a credential. If it fails, use its HTTP status and safe error name in the table below.

Open `http://127.0.0.1:8765/api/phone-share/config` on that computer. It must show `"configured": true`; this response never exposes the secrets. Then open the dashboard at `http://127.0.0.1:8765/`.

Close the terminal when finished to remove its process-local environment. For persistent storage, use operating-system secret storage—not a tracked repository file, `setx`, or a plaintext batch file.

### Step 11 — Complete the first-share acceptance test

1. On the local dashboard, click **View on Phone**.
2. Select **5 minutes** for this first test and click **Create secure QR code**.
3. Wait for **Secure phone sharing is active**. The QR code appears only after the first encrypted upload succeeds. Confirm that **Last upload** has a current time.
4. Scan the QR code with the phone camera. Use the phone's ordinary browser—no app is required.
5. Complete the expected Cloudflare login or one-time PIN with the exact email in the viewer **Allow** policy.
6. Confirm that the phone shows the dashboard and its last-upload time updates automatically.
7. Keep the local dashboard tab and server running while you want new uploads. Closing them stops future uploads; the last encrypted snapshot remains only until successful revocation or expiry.
8. Click **Stop sharing**. Refresh the phone and confirm it reports that the share was stopped or expired. Then create one fresh share to prove the setup can be reused.

Setup is complete only when all of these are true:

- `/view` loads and `/api/auth` requires the allowed viewer identity;
- the local config endpoint says `"configured": true`;
- the QR appears after a successful first upload;
- the allowed phone can decrypt and refresh the dashboard; and
- **Stop sharing** successfully revokes the test share.

Keep the QR code and **Copy private link** output private. They contain the decryption key. Anyone who has the link and can satisfy your Access viewer policy can decrypt that share until it is revoked or expires.

## Troubleshooting

Start with the exact symptom shown on screen; do not rotate every value at once.

| Symptom | What it means and what to check |
|---|---|
| **Sharing is not configured** or local config says `false` | One of the four local values is missing or malformed. Check the HTTPS base URL has no path, upload token is at least 32 characters, Client ID at least 16, and Client Secret at least 32. Set them in the same terminal and restart the server. |
| Local relay returns HTTP `502` | Stop the dashboard, set the four values in that same terminal, and run `py -3 dashboard/phone_share_preflight.py`. Use the reported safe error below. First compare Worker `UPLOAD_TOKEN` with local `RHMRA_PHONE_SHARE_UPLOAD_TOKEN` character for character; then check the local Client ID/Secret and Access **Service Auth** policy. |
| Cloudflare login loops, Access denies the phone, or Access returns `401` | Check the protected destination is exactly `/api/*`, the viewer policy contains the correct email, an identity provider is available, and the application was finally created. An initial login screen is normal. |
| Worker JSON says `unauthorized` | The upload bearer token does not match, or Worker `ACCESS_SERVICE_CLIENT_ID` does not match the service token reaching Access. |
| Worker JSON says `invalid_access_configuration` | `ACCESS_TEAM_DOMAIN` or `ACCESS_AUD` is missing or malformed. Correct it and click **Deploy**. |
| Worker JSON says `access_keys_unavailable` | Check the full team URL, deploy the current Worker code, and confirm the `global_fetch_strictly_public` flag is present. A temporary Cloudflare signing-key outage can also cause this fail-closed response. |
| Worker JSON says `access_keys_invalid` | Cloudflare returned an unexpected signing-key document. Recheck the team URL and retry; if it persists, inspect Worker logs. |
| Worker JSON says `write_auth_not_configured` | `UPLOAD_TOKEN` or `ACCESS_SERVICE_CLIENT_ID` is missing/short, or `WRITE_AUTH_MODE` is not `bearer-and-service-token`. |
| Preflight says `cloudflare_access_denied` | Cloudflare Access rejected the local uploader before the Worker could answer. Check the local Client ID and Client Secret, confirm the **Service Auth** policy selects that service token, and confirm the Access destination is exactly the Worker hostname plus `/api/*`. |
| Preflight says `network_or_tls_error` | The computer could not establish the HTTPS request. Check internet and DNS access, confirm the Worker base URL is exact and has no path, and retry. Do not disable TLS verification. |
| Preflight says `non_json_worker_response` | The URL returned an unexpected page, commonly an Access login/error page or a response from the wrong host. Recheck the base URL, `/api/*` Access application, uploader policy, and latest Worker deployment. |
| Worker JSON says `storage_not_configured` | The `SHARE_SESSION` Durable Object binding is missing. Redeploy from the current Worker package and verify **Bindings**. |
| HTTP `404` | The local base URL is wrong. It must be only `https://name.account.workers.dev`, without `/api` or `/view`. |
| QR never appears | The first encrypted upload did not succeed. Read the red message in the dialog and resolve the corresponding local `502` or Worker JSON error before scanning. |
| Phone data stops refreshing | Keep the local dashboard tab and server running, verify network access, check **Last upload**, and confirm the share has not expired. |
| A previous share cannot be revoked | Repair the credentials and click **Stop previous share** again. If revocation cannot be restored, its discarded key prevents this browser from resuming uploads and the ciphertext expires automatically at its original time. |
| Cloudflare warns about updating Wrangler configuration | Click **Deploy**, but never put secret values in the tracked Wrangler file. The warning does not mean the secret failed to save. |

## Rotating or recovering credentials

- **Lost or exposed upload token:** stop the active share if possible; generate a new random value; replace Worker `UPLOAD_TOKEN`; click **Deploy**; set the identical local `RHMRA_PHONE_SHARE_UPLOAD_TOKEN`; restart the dashboard; repeat the 5-minute test. If the old share cannot be revoked after rotation, it still expires automatically.
- **Expired or replaced service token:** create the replacement and update the Access **Service Auth** policy. If its Client ID changed, also update Worker `ACCESS_SERVICE_CLIENT_ID` and click **Deploy**. Update both local Client ID and Client Secret, restart, and repeat the test.
- **Lost Client Secret:** Cloudflare cannot show it again. Rotate or replace the service token; do not guess it.
- **Changed team URL or AUD:** update the corresponding Worker secret, click **Deploy**, and repeat Steps 9 and 11.
- **Leaked QR or private link:** click **Stop sharing** and create a new share. Never reuse or publish the old QR screenshot.

Record the service-token expiration date and renew it before that date.

## Updating or removing the relay

Cloudflare's generated deployment repository is a snapshot of `phone-share-worker/`; it does **not** automatically receive later RHMRA fixes. Updates are separate from first-time setup. For a manual update, install Git and Node.js 20 or newer, then:

1. Download or pull the current RHMRA release.
2. Clone the private repository Cloudflare generated in Step 3, or open its existing local clone. Do not perform these steps in the main RHMRA repository.
3. Record the generated repository's current `"name"` in `wrangler.jsonc`. It should be `rhmra-phone-share` on the beginner path. If an older installation uses a different Worker name, restore that existing name after the copy so the update cannot create a second Worker.
4. Copy only `src/`, `test/`, `package.json`, `package-lock.json`, `wrangler.jsonc`, `README.md`, `.gitignore`, `.dev.vars.example`, and `local-dev.vars.example` from the new RHMRA release's `phone-share-worker/` folder into the generated repository.
5. Never copy `node_modules/`, `.wrangler/`, or `.dev.vars`. None belongs in Git.
6. Open a terminal in the generated repository and run `npm ci`, then `npm test`, `git status`, and `git diff`. The diff must contain code/configuration only—never a token, Client Secret, AUD, team URL, QR link, or dashboard data.
7. Commit and push. Cloudflare builds the new production version automatically.
8. Confirm deployment success, `SHARE_SESSION`, the four Worker secret **names** (their values stay encrypted in Cloudflare), `/view`, the uploader preflight, and a 5-minute share.

Advanced users may instead deploy this directory with Wrangler as described below.

To remove phone sharing completely:

1. Stop any active share and close the local dashboard.
2. Remove the four local values from your terminal or operating-system secret store.
3. Delete the Cloudflare Access application and uploader service token.
4. Delete the Worker and its generated deployment repository.
5. Optionally revoke the Cloudflare Workers and Pages GitHub app if nothing else uses it.

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

The diagnostic helper calls `POST /api/preflight` with the same bearer and Cloudflare service-token headers as an upload. It returns `204 No Content` only after write authorization and the `SHARE_SESSION` binding pass; it never opens a Durable Object or accepts dashboard data.

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

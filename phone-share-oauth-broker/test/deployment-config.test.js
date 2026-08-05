import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function read(name) {
  return readFileSync(join(ROOT, name), "utf8");
}

test("deployment pins the public client id but never stores the private secret", () => {
  const configuration = JSON.parse(read("wrangler.jsonc"));
  assert.equal(configuration.name, "rhmra-google-oauth-broker");
  assert.equal(configuration.preview_urls, false);
  assert.equal(configuration.logpush, false);
  assert.deepEqual(configuration.observability, {
    enabled: false,
    logs: { invocation_logs: false },
  });
  assert.deepEqual(configuration.ratelimits, [{
    name: "OAUTH_RATE_LIMITER",
    namespace_id: "13490783057",
    simple: { limit: 30, period: 60 },
  }]);
  assert.deepEqual(configuration.compatibility_flags, ["global_fetch_strictly_public"]);
  assert.match(configuration.vars.GOOGLE_DESKTOP_CLIENT_ID, /\.apps\.googleusercontent\.com$/);
  assert.equal("GOOGLE_DESKTOP_CLIENT_SECRET" in configuration.vars, false);
  assert.doesNotMatch(read("wrangler.jsonc"), /GOCSPX-/);
});

test("local secret files are excluded from source control", () => {
  assert.match(read(".gitignore"), /^\.dev\.vars$/m);
  assert.match(read(".dev.vars.example"), /^GOOGLE_DESKTOP_CLIENT_SECRET=/m);
});

test("maintainer tooling pins the OS keyring and narrows build scripts", () => {
  const packageDocument = JSON.parse(read("package.json"));
  assert.equal(packageDocument.devDependencies["@napi-rs/keyring"], "1.3.0");
  assert.equal(packageDocument.packageManager, "pnpm@11.20.0");
  assert.match(read("pnpm-lock.yaml"), /'@napi-rs\/keyring':\r?\n\s+specifier: 1\.3\.0\r?\n\s+version: 1\.3\.0/);
  assert.match(read("pnpm-workspace.yaml"), /^allowBuilds:\r?\n  esbuild: true\r?\n  workerd: true\r?\n?$/);
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const WORKER_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function read(name) {
  return readFileSync(join(WORKER_ROOT, name), 'utf8');
}

function dotenvKeys(text) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
    .map((line) => line.split('=', 1)[0]);
}

test('deploy-button secrets exclude local authentication overrides', () => {
  assert.deepEqual(dotenvKeys(read('.dev.vars.example')), ['UPLOAD_TOKEN']);
});

test('production and local authentication modes remain deliberately separate', () => {
  const configuration = JSON.parse(read('wrangler.jsonc'));
  assert.equal(configuration.vars.WRITE_AUTH_MODE, 'bearer-and-service-token');
  assert.equal(configuration.vars.VERIFY_ACCESS_JWT, 'true');

  const localTemplate = read('local-dev.vars.example');
  assert.match(localTemplate, /^WRITE_AUTH_MODE=bearer$/m);
  assert.match(localTemplate, /^VERIFY_ACCESS_JWT=false$/m);
  assert.match(localTemplate, /^UPLOAD_TOKEN=replace-with-at-least-32-random-characters$/m);
});

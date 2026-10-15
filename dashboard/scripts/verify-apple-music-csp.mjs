import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { chromium } from 'playwright';

const appleScript = 'https://js-cdn.music.apple.com/musickit/v3/musickit.js';
const appleApi = 'https://api.music.apple.com/v1/__wrzdj_csp_probe__';

function policyFromTemplate(template, overlay) {
  const policies = [...template.matchAll(/add_header Content-Security-Policy "([^"]+)"/g)];
  const policy = policies.find(([, value]) =>
    overlay ? value.includes('frame-ancestors *') : value.includes("frame-ancestors 'none'"),
  )?.[1];
  assert.ok(policy, `missing ${overlay ? 'overlay' : 'page'} CSP in nginx template`);
  return policy.replaceAll('${API_DOMAIN}', 'api.example.test');
}

const template = await readFile('../deploy/nginx/app.conf.template', 'utf8');
const pagePolicy = policyFromTemplate(template, false);
const overlayPolicy = policyFromTemplate(template, true);
assert.match(pagePolicy, /script-src[^;]*https:\/\/js-cdn\.music\.apple\.com/);
assert.match(pagePolicy, /connect-src[^;]*https:\/\/api\.music\.apple\.com/);
assert.doesNotMatch(overlayPolicy, /https:\/\/(?:js-cdn\.music|api\.music)\.apple\.com/);

const html = (policy) => `<!doctype html><meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="${policy.replace(/;\s*frame-ancestors[^;]*/, '')}">
  <script>window.cspProbe = 'ready'</script>`;
const server = createServer((request, response) => {
  const overlay = request.url === '/overlay';
  response.writeHead(200, { 'Content-Type': 'text/html' });
  response.end(html(overlay ? overlayPolicy : pagePolicy));
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const address = server.address();
assert.ok(address && typeof address === 'object');

let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const blocked = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().includes('Content Security Policy')) {
      blocked.push(message.text());
    }
  });
  await page.route(appleApi, (route) =>
    route.fulfill({
      status: 200,
      headers: { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' },
      body: '{"data":[]}',
    }),
  );

  await page.goto(`http://127.0.0.1:${address.port}/`);
  await page.addScriptTag({ url: appleScript });
  await page.waitForFunction(() => typeof window.MusicKit === 'object', null, { timeout: 30000 });
  const apiRequestWorked = await page.evaluate(async (url) => {
    const response = await fetch(url);
    return response.ok;
  }, appleApi);
  assert.equal(apiRequestWorked, true, 'normal-page CSP should allow the Apple API connection');
  assert.deepEqual(blocked, [], 'normal-page CSP should not block MusicKit bootstrap');

  const overlay = await browser.newPage();
  const overlayBlocked = new Promise((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error('timed out waiting for the overlay CSP to block MusicKit')),
      10000,
    );
    overlay.on('console', (message) => {
      if (message.type() === 'error' && message.text().includes('Content Security Policy')) {
        clearTimeout(timeout);
        resolve(message.text());
      }
    });
  });
  await overlay.goto(`http://127.0.0.1:${address.port}/overlay`);
  await overlay.addScriptTag({ url: appleScript }).catch(() => undefined);
  assert.match(await overlayBlocked, /script-src/);
  assert.equal(await overlay.evaluate(() => typeof window.MusicKit), 'undefined');

  const evidence = {
    result: 'pass',
    realMusicKitScript: appleScript,
    globalAfterLoad: await page.evaluate(() => typeof window.MusicKit),
    mockedAppleApiProbe: apiRequestWorked,
    overlayDeniedScript: true,
    accountGrantAttempted: false,
  };
  await mkdir('.ak/evidence', { recursive: true });
  await writeFile('.ak/evidence/apple-music-csp-browser.json', `${JSON.stringify(evidence, null, 2)}\n`);
  console.log('Apple Music CSP browser check passed: real MusicKit loaded on normal pages; overlay blocked it.');
} finally {
  await browser?.close();
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
}

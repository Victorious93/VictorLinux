// Shared harness for feature-screen browser tests: real Control Center on a temp data dir + Chromium.
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const root = path.resolve(__dirname, '..', 'airootfs/usr/local');
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function withControlCenter(port, extraEnv, seed, fn) {
 const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'victorext-'));
 const env = {...process.env, HOME: tmp, XDG_CACHE_HOME: tmp + '/cache', VICTOR_DATA: tmp + '/data', VICTOR_RUN_DIR: tmp + '/run',
   VICTOR_CC_PORT: String(port), VICTOR_FLEET_DIR: tmp + '/fleet', ...extraEnv};
 fs.mkdirSync(tmp + '/data/models', {recursive: true});
 if (seed) await seed(tmp, env);
 const cc = spawn('python3', ['-I', root + '/lib/victorlinux/control_center.py'], {env});
 let output = '', browser;
 cc.stdout.on('data', b => { output += b; }); cc.stderr.on('data', b => { output += b; });
 try {
  const tokenFile = tmp + '/cache/victorlinux/cc.token';
  for (let i = 0; i < 100 && !fs.existsSync(tokenFile) && cc.exitCode === null; i++) await sleep(100);
  assert.ok(fs.existsSync(tokenFile), 'Server startup failed: ' + output);
  await sleep(300);
  const token = fs.readFileSync(tokenFile, 'utf8').trim();
  const args = (extraEnv && extraEnv.CHROME_ARGS) ? extraEnv.CHROME_ARGS.split(' ') : [];
  browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined, args});
  const page = await browser.newPage({viewport: {width: 1366, height: 900}, permissions: args.length ? ['microphone'] : []});
  const errors = [], foreign = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (!new RegExp(`^(http://127\\.0\\.0\\.1:${port}/|data:|blob:)`).test(r.url())) foreign.push(r.url()); });
  await page.route('**/api/storage', route => route.fulfill({json: {state: {mode: 'missing', message: 'none'}, partitions: []}}));
  await page.goto(`http://127.0.0.1:${port}/#t=${token}`);
  await page.waitForSelector('#nav button');
  await fn(page, tmp, token);
  // brand: dark background, no light surfaces on the visible tab, no external requests, no JS errors
  assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(11, 15, 26)');
  const light = await page.evaluate(() => [...document.querySelectorAll('section.on *')].filter(el => el.offsetParent !== null).map(el => {
    const [r, g, b, a = 1] = (getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || []).map(Number);
    return {el: el.tagName + '#' + el.id, r, g, b, a}; }).filter(c => c.a > 0.3 && c.r > 200 && c.g > 200 && c.b > 200).map(c => c.el));
  assert.deepEqual(light, [], 'light backgrounds');
  assert.deepEqual(errors, [], 'JS errors');
  assert.deepEqual(foreign, [], 'external requests');
  return output;
 } finally {
  if (browser) await browser.close();
  cc.kill();
  await new Promise(resolve => { if (cc.exitCode !== null) resolve(); else cc.once('exit', resolve); });
  fs.rmSync(tmp, {recursive: true, force: true});
 }
}

async function shots(page, name) {
 const dir = process.env.VICTOR_SHOTS_DIR;
 if (!dir) return;
 fs.mkdirSync(dir, {recursive: true});
 for (const [w, h] of [[1366, 900], [390, 844]]) {
  await page.setViewportSize({width: w, height: h}); await sleep(250);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `overflow at ${w}px`);
  await page.screenshot({path: path.join(dir, `${name}-${w}.png`), fullPage: true});
 }
 await page.setViewportSize({width: 1366, height: 900});
}

module.exports = {withControlCenter, shots, sleep, assert, fs, path};

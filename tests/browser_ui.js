// Real Control Center + Chromium: CSP, unsaved forms, brand palette, status dashboard, keyboard access and responsive tabs.
// VICTOR_SHOTS_DIR=dir saves per-tab screenshots at 1366 and 390 px.
// npm install --no-save playwright; npx playwright install chromium; node tests/browser_ui.js
// PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH selects an already installed Chromium.
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const root = path.resolve(__dirname, '..', 'airootfs/usr/local');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'victorui-'));
const env = {...process.env, HOME: tmp, XDG_CACHE_HOME: tmp + '/cache',
  VICTOR_DATA: tmp + '/data', VICTOR_RUN_DIR: tmp + '/run', VICTOR_CC_PORT: '18788',
   VICTOR_FLEET_DIR: tmp + '/fleet', VICTOR_FLEET_BIN: path.resolve(__dirname, '..', 'fleet/bin/victor-fleet'), VICTOR_FLEET_LIB: path.resolve(__dirname, '..', 'fleet')};
fs.mkdirSync(tmp + '/data/models', {recursive: true});
fs.mkdirSync(tmp + '/notes', {recursive: true});
fs.writeFileSync(tmp + '/notes/lease.md', '# Lease\n\nPets: one cat is allowed with a deposit of 300 dollars.\n');
fs.writeFileSync(tmp + '/notes/recipe.txt', 'Bread needs flour, water, salt and yeast.');


// Smallest valid GGUF v3 (no tensors, no metadata) so the model library has a real file to hash.
const ggufHeader = Buffer.alloc(24); ggufHeader.write('GGUF', 0); ggufHeader.writeUInt32LE(3, 4);
fs.writeFileSync(tmp + '/data/models/tiny-Q4_K_M.gguf', Buffer.concat([ggufHeader, Buffer.alloc(64 * 1024)]));
const cc = spawn('python3', ['-I', root + '/lib/victorlinux/control_center.py'], {env});
let output = '';
cc.stdout.on('data', b => { output += b; });
cc.stderr.on('data', b => { output += b; });
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
 let browser;
 try {
  const tokenFile = tmp + '/cache/victorlinux/cc.token';
  for (let i = 0; i < 100 && !fs.existsSync(tokenFile) && cc.exitCode === null; i++) await sleep(100);
  assert.ok(fs.existsSync(tokenFile), 'Server startup failed: ' + output);
  const token = fs.readFileSync(tokenFile, 'utf8').trim();
  browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined, args: JSON.parse(process.env.PLAYWRIGHT_CHROMIUM_ARGS || "[]")});
  const page = await browser.newPage({colorScheme: 'light', viewport: {width: 1366, height: 900}});
  const errors = [], foreign = [];
  page.on('pageerror', e => errors.push(e.message));
  // No external fonts, CDNs or network calls: every request stays on the local Control Center origin.
  page.on('request', r => { if (!/^(http:\/\/127\.0\.0\.1:18788\/|data:|blob:)/.test(r.url())) foreign.push(r.url()); });
  // The source checkout has no root helper installed: fixture this one read-only response.
  await page.route('**/api/storage', route => route.fulfill({json: {
   state: {mode: 'missing', message: 'No persistent storage selected.'}, partitions: [],
  }}));
  await page.goto('http://127.0.0.1:18788/#t=' + token);
  await page.waitForFunction(() => !['storage…', 'storage...'].includes(document.querySelector('#storagePill').textContent));
  assert.equal(await page.locator('#nav button').count(), await page.locator('main section').count());
  assert.ok(await page.locator('#nav button').count() >= 7);
  const csp = (await page.request.get('http://127.0.0.1:18788/')).headers()['content-security-policy'];
  assert.match(csp, /script-src[^;]*nonce-/);
  assert.doesNotMatch(csp, /script-src[^;]*unsafe-inline/);
  console.log('PASS real server, authenticated status, CSP nonce');
  // Fleet tab: start / pair / stop the real coordinator through the UI
  await page.locator('[data-t="fleet"]').click();
  await page.waitForFunction(() => /stopped/i.test(document.getElementById('fOut').textContent));
  await page.click('#fStart');
  await page.waitForFunction(() => /running on 127\.0\.0\.1/.test(document.getElementById('fOut').textContent), null, {timeout: 15000});
  await page.click('#fPair');
  await page.waitForFunction(() => /Pairing secret/.test(document.getElementById('fOut').textContent) && /[0-9a-f]{64}/.test(document.getElementById('fOut').textContent));
  await page.click('#fStop'); await sleep(1500); await page.click('#fRef');
  await page.waitForFunction(() => /stopped/i.test(document.getElementById('fOut').textContent));
  console.log('PASS fleet tab start/pair/stop');
  // Mesh tab: off by default, nothing preset, bad input refused before anything privileged runs, key field cleared.
  await page.locator('[data-t="mesh"]').click();
  await page.waitForFunction(() => /Not joined|not installed/.test(document.getElementById('meshState').textContent)
    && document.querySelectorAll('.meshT:disabled').length === 5);
  assert.equal(await page.locator('#meshUrl').inputValue(), '');
  assert.equal(await page.locator('#meshKey').getAttribute('type'), 'password');
  assert.equal(await page.locator('.meshT:checked').count(), 0);
  assert.equal(await page.locator('.meshT:disabled').count(), 5);
  await page.fill('#meshUrl', 'http://headscale.example.com');
  await page.fill('#meshKey', 'abcdefghijklmnop1234');
  await page.click('#bMeshJoin');
  await page.waitForFunction(() => /https/.test(document.getElementById('toast').textContent));
  assert.equal(await page.locator('#meshKey').inputValue(), '');
  const bad = await page.evaluate(() => api('/api/mesh', {action: 'join', url: 'https://hs.example.com', key: 'short'}));
  assert.ok(/pre-auth key/.test(bad.error) && !/short/.test(bad.error));
  assert.ok((await page.evaluate(() => api('/api/mesh', {action: 'set', toggle: 'rm -rf', on: true}))).error);
  console.log('PASS mesh tab: off by default, URL/key validated, key never echoed');
  // Documents: refuse system folders, add a chosen folder, index locally, ask without a model -> cited passages only.
  await page.locator('[data-t="docs"]').click();
  await page.waitForFunction(() => /No folders yet/.test(document.getElementById('dSrc').textContent));
  await page.fill('#dPath', '/etc'); await page.click('#bDocAdd');
  await page.waitForFunction(() => /system or private/.test(document.getElementById('toast').textContent));
  await page.fill('#dPath', tmp + '/cache/victorlinux'); await page.click('#bDocAdd');
  await page.waitForFunction(() => document.getElementById('toast').textContent.split('system or private').length > 2);
  await page.fill('#dPath', tmp + '/notes'); await page.click('#bDocAdd');
  await page.waitForFunction(() => /notes/.test(document.getElementById('dSrc').textContent));
  assert.equal(await page.locator('#dSrc [data-docrm]').count(), 1);
  await page.click('#bDocIndex');
  await page.waitForFunction(() => /Indexed: 2 new or changed/.test(document.getElementById('dJob').textContent), null, {timeout: 15000});
  assert.match(await page.locator('#dJob').innerText(), /keyword only/);
  await page.fill('#dQ', 'Is a cat allowed as a pet?'); await page.click('#bAsk');
  await page.waitForSelector('#dHits .hit');
  assert.match(await page.locator('#dHits .hit').first().innerText(), /lease\.md/);
  assert.match(await page.locator('#dAnswer').innerText(), /Start a model/);
  if (process.env.VICTOR_SHOTS_DIR) { fs.mkdirSync(process.env.VICTOR_SHOTS_DIR, {recursive: true});
   await page.screenshot({path: path.join(process.env.VICTOR_SHOTS_DIR, 'documents-1366.png'), fullPage: true}); }
  console.log('PASS documents tab: refuses /etc and the session-token folder, indexes a chosen folder, cites passages offline');
  await page.locator('[data-t="remote"]').click();
  await page.locator('#rEn').check(); await page.locator('#rTls').check();
  await page.locator('#rBind').fill('100.64.0.9'); await page.locator('#rPort').fill('9090');
  await page.locator('#tlsHost').focus(); await page.evaluate(() => refresh());
  assert.equal(await page.locator('#rBind').inputValue(), '100.64.0.9');
  assert.equal(await page.locator('#rPort').inputValue(), '9090');
  assert.equal(await page.locator('#rEn').isChecked(), true);
  assert.equal(await page.locator('#rTls').isChecked(), true);
  console.log('PASS polling preserves unsaved remote settings');
  await page.locator('[data-t="models"]').click();
  assert.equal(await page.locator('#sCtxAuto').isChecked(), true, 'new installs default to automatic context');
  assert.equal(await page.locator('#sCtx').isDisabled(), true);
  await page.locator('#sCtxAuto').uncheck(); assert.equal(await page.locator('#sCtx').isDisabled(), false);
  await page.locator('#sCtx').fill('8192');
  await page.evaluate(() => refresh()); assert.equal(await page.locator('#sCtx').inputValue(), '8192');
  await page.locator('#bSave').click();
  await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('Settings saved'));
  await page.reload(); await page.waitForFunction(() => document.querySelector('#sCtx').value === '8192');
  assert.equal(await page.locator('#sCtxAuto').isChecked(), false, 'manual context choice persists');
  console.log('PASS runtime settings save and reload');
  // Model library: quantisation from the name, background hash, remember settings, use with saved settings
  await page.waitForFunction(() => document.querySelectorAll('#libTable tr[data-path]').length === 1);
  const libRow = page.locator('#libTable tr[data-path]');
  assert.match(await libRow.innerText(), /tiny-Q4_K_M\.gguf[\s\S]*Q4_K_M[\s\S]*Not hashed yet[\s\S]*Never run/);
  await page.click('#bHashAll');
  await page.waitForFunction(() => /Hashed/.test(document.querySelector('#libTable tr[data-path]').innerText), null, {timeout: 10000});
  await libRow.locator('button[data-lib="remember"]').click();
  await page.waitForFunction(() => /ctx 8192/.test(document.querySelector('#libTable tr[data-path]').innerText));
  await libRow.locator('button[data-lib="apply"]').click();
  await page.waitForFunction(() => /selected/.test(document.querySelector('#libTable tr[data-path]').innerText));
  assert.match(await page.locator('#mSel').inputValue(), /tiny-Q4_K_M\.gguf$/);
  console.log('PASS model library: quant, background hash, remembered settings, apply');
  await page.locator('[data-t="dash"]').click();
  await page.locator('#bAdvice').click();
  await page.waitForFunction(() => document.querySelector('#advice').textContent.includes('Memory pool:'));
  assert.equal(await page.locator('#advice').textContent().then(t => t.includes('Optional catalog (not downloaded)')), true);
  console.log('PASS host memory pool and ranked-model advice render');
  // Check this PC: one click from the dashboard, verdict, fixes, saved report; speed test refuses without a loaded model
  await page.click('#bFullCheck');
  await page.waitForSelector('#t-logs.on');
  await page.waitForSelector('#pcVerdict', {timeout: 20000});
  assert.ok(await page.locator('#pcList .chk').count() >= 4);
  assert.match(await page.locator('#pcList').innerText(), /Inference backend[\s\S]*How to fix/);
  assert.match(await page.locator('#pcVerdict').innerText(), /saved as pc-check-[0-9a-f]{12}-\d{8}-\d{6}\.json/);
  assert.ok(fs.readdirSync(tmp + '/data/reports').some(f => /^pc-check-.*\.json$/.test(f)) && fs.existsSync(tmp + '/data/reports/profiles.json'));
  await page.click('#bSpeed');
  await page.waitForFunction(() => /Start a model first/.test(document.querySelector('#toast').textContent));
  console.log('PASS check this PC: verdict, fixes, saved profile, speed test needs a loaded model');
  // Storage tab with several drives at once: internal SSD, external 2 TB exFAT drive, FAT32 stick, locked and duplicate volumes
  await page.unroute('**/api/storage');
  const part = (o) => Object.assign({uuid: 'U', label: '', fstype: 'ext4', size: 1e12, free: 5e11, kind: 'internal-ssd', removable: false, model: '', serial: '',
   mounted: true, mountpoint: '/mnt/victor-vol/x', mounted_ro: false, device_ro: false, locked: false, supported: true, persist_ok: true, ro_ok: true, duplicate_uuid: false}, o);
  const dest = (role, o) => ({role, subpath: '', ref: {uuid: o.uuid}, status: 'active', message: '', path: o.path, writable: true, fstype: o.fstype, free: o.free, persistent: true});
  await page.route('**/api/storage', route => route.fulfill({json: {
   state: {mode: 'persistent', message: 'Persistent storage active on /dev/sdc3 (VICTORDATA).'},
   partitions: [
    part({path: '/dev/sdc3', uuid: 'VD-1', label: 'VICTORDATA', size: 6e10, kind: 'usb-storage', removable: true, model: 'Cruzer', serial: 'SD1', mountpoint: '/mnt/victordata'}),
    part({path: '/dev/nvme0n1p2', uuid: 'SSD-1', label: 'Internal SSD', kind: 'internal-nvme', model: 'Samsung SSD 990', persist_ok: true}),
    part({path: '/dev/sdb1', uuid: 'EXF-1', label: 'Seagate 2TB', fstype: 'exfat', size: 2e12, free: 1.4e12, kind: 'usb-hdd', removable: true, model: 'Expansion', serial: 'NA9', persist_ok: false}),
    part({path: '/dev/sdd1', uuid: 'FAT-1', label: 'CARD', fstype: 'vfat', size: 6.4e10, kind: 'sd-card', persist_ok: false}),
    part({path: '/dev/sde1', uuid: 'LUKS-1', fstype: 'crypto_LUKS', locked: true, supported: false, persist_ok: false, ro_ok: false, mounted: false, mountpoint: null}),
    part({path: '/dev/sdf1', uuid: 'DUP-1', label: 'clone', duplicate_uuid: true, persist_ok: true}),
   ],
   summary: {state: {mode: 'persistent'}, destinations: {
    models: [dest('models', {uuid: 'EXF-1', path: '/mnt/victor-vol/EXF-1/LLM library', fstype: 'exfat', free: 1.4e12})],
    documents: [dest('documents', {uuid: 'SSD-1', path: '/mnt/victor-vol/SSD-1/Documents', fstype: 'ext4', free: 5e11})],
    backups: [Object.assign(dest('backups', {uuid: 'GONE', path: null, fstype: 'ntfs', free: null}), {status: 'missing', message: 'The drive with filesystem GONE (Backup HDD) is not connected.'})]}}}}));
  await page.locator('[data-t="storage"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#stTable tr').length === 7);
  const dsText = await page.locator('#dsTable').innerText();
  assert.match(dsText, /models[\s\S]*active · persistent[\s\S]*LLM library/);
  assert.match(dsText, /backups[\s\S]*missing[\s\S]*not connected/);
  const drivesText = await page.locator('#stTable').innerText();
  assert.match(drivesText, /Seagate 2TB[\s\S]*exfat/); assert.match(drivesText, /files < 4 GiB/); assert.match(drivesText, /locked \(encrypted\)/); assert.match(drivesText, /duplicate UUID/);
  assert.equal(await page.locator('#stTable button[data-use]').count(), 2);          // exFAT/FAT32/locked/duplicate cannot hold app state
  assert.equal(await page.locator('#dsDrive option').count(), 5);                     // locked volume is not offered as a destination
  assert.match(await page.locator('#stState').innerText(), /Persistent/);
  console.log('PASS storage tab: multiple drives, destinations, missing drive, FAT32/locked/duplicate flags');
  // API keys + opt-in phone pairing: off by default, key shown once, QR carries a single-use token and never an API key.
  await page.locator('[data-t="remote"]').click();
  await page.waitForFunction(() => /Pairing is off/.test(document.getElementById('pState').textContent));
  await page.waitForFunction(() => /Off/i.test(document.getElementById('inState').textContent));
  assert.match(await page.locator('#btState').innerText(), /Off/i);
  assert.equal(await page.locator('#btCard').count(), 1);
  assert.equal(await page.locator('#inCard').count(), 1);
  const bg = await page.locator('#inCard').evaluate(el => getComputedStyle(el).backgroundColor);
  assert.notEqual(bg, 'rgb(255, 255, 255)');
  assert.equal(await page.locator('#pQr svg').count(), 0);
  await page.click('#bPair');
  await page.waitForFunction(() => /needs remote access turned on with TLS/.test(document.getElementById('toast').textContent));
  await page.fill('#kName', 'Laptop'); await page.fill('#kExp', '30'); await page.fill('#kRpm', '60'); await page.click('#bKNew');
  await page.waitForSelector('#kNewVal');
  const apiKey = (await page.locator('#kNewVal').innerText()).trim();
  assert.match(apiKey, /^victor-[A-Za-z0-9_-]{40,}$/);
  await page.waitForFunction(() => /Laptop/.test(document.getElementById('kTable').textContent) && /60\/min/.test(document.getElementById('kTable').textContent));
  require('node:child_process').execFileSync('openssl', ['req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
    '-keyout', tmp + '/data/server/tls/key.pem', '-out', tmp + '/data/server/tls/cert.pem', '-days', '2', '-subj', '/CN=victorlinux'],
    {stdio: 'ignore', env: {...process.env, OPENSSL_CONF: '/dev/null'}, cwd: (fs.mkdirSync(tmp + '/data/server/tls', {recursive: true}), tmp)});
  assert.equal((await page.evaluate(() => api('/api/remote', {enabled: true, bind: '100.64.0.9', port: 9443, tls: true}))).ok, true);
  await page.evaluate(() => loadKeys());
  await page.waitForFunction(() => document.getElementById('pHost').value === '100.64.0.9');
  await page.click('#bPair');
  await page.waitForSelector('#pQr svg');
  const pq = await page.locator('#pQr').innerText();
  assert.match(pq, /https:\/\/100\.64\.0\.9:9443/);
  assert.match(pq, /([0-9a-f]{2}:){31}[0-9a-f]{2}/);
  assert.equal((await page.locator('#pQr').innerHTML()).includes(apiKey), false, 'QR area must never contain an API key');
  await page.waitForFunction(() => /Pairing window open/.test(document.getElementById('pState').textContent));
  if (process.env.VICTOR_SHOTS_DIR) { fs.mkdirSync(process.env.VICTOR_SHOTS_DIR, {recursive: true});
   await page.screenshot({path: path.join(process.env.VICTOR_SHOTS_DIR, 'api-keys-pairing-1366.png'), fullPage: true}); }
  await page.click('#bPairStop');
  await page.waitForFunction(() => /Pairing is off/.test(document.getElementById('pState').textContent) && !document.querySelector('#pQr svg'));
  page.once('dialog', d => d.accept());
  await page.locator('#kTable [data-krev]').first().click();
  await page.waitForFunction(() => !/Laptop/.test(document.getElementById('kTable').textContent));
  assert.equal((await page.evaluate(() => api('/api/remote/revoke', {}))).ok, true);
  await page.evaluate(() => refresh());
  console.log('PASS API keys (named, expiry, limit, shown once, revoke) and opt-in phone pairing QR without an API key');


  // Storage health + encrypted backup round trip (gpg, passphrase never echoed), guided restore with confirmation.
  await page.locator('[data-t="storage"]').click();
  await page.click('#bHealth');
  await page.waitForFunction(() => /VictorLinux data/.test(document.getElementById('hList').textContent));
  assert.match(await page.locator('#hSum').innerText(), /OK|Warning|Problem|Unknown/);
  await page.fill('#bkPass', 'short'); await page.fill('#bkPass2', 'short'); await page.click('#bBackup');
  await page.waitForFunction(() => /at least 10 characters/.test(document.getElementById('toast').textContent));
  await page.fill('#bkPass', 'blue lantern river 42'); await page.fill('#bkPass2', 'blue lantern river 42'); await page.click('#bBackup');
  await page.waitForFunction(() => /Encrypted backup saved: .* no secrets/.test(document.getElementById('toast').textContent), null, {timeout: 20000});
  assert.equal(await page.locator('#bkPass').inputValue(), '');
  const bk = fs.readdirSync(tmp + '/data/backups').filter(n => n.endsWith('.vlbak'));
  assert.equal(bk.length, 1);
  assert.ok(!fs.readFileSync(tmp + '/data/backups/' + bk[0]).includes('ctx'), 'backup must be encrypted');
  await page.locator('#bkTable input[name="bk"]').first().check();
  assert.equal(await page.locator('#bkRestoreRow').isVisible(), false);
  await page.fill('#bkRPass', 'wrong passphrase!'); await page.click('#bInspect');
  await page.waitForFunction(() => /Wrong passphrase/.test(document.getElementById('bkOut').textContent), null, {timeout: 20000});
  assert.equal(await page.locator('#bkRestoreRow').isVisible(), false);
  await page.fill('#bkRPass', 'blue lantern river 42'); await page.click('#bInspect');
  await page.waitForFunction(() => /Verified: \d+ files/.test(document.getElementById('bkOut').textContent), null, {timeout: 20000});
  assert.match(await page.locator('#bkOut').innerText(), /config\/settings\.json/);
  assert.doesNotMatch(await page.locator('#bkOut').innerText(), /secrets\.json/);
  await page.click('#bRestore');
  await page.waitForFunction(() => /Tick the box/.test(document.getElementById('toast').textContent));
  await page.check('#bkSure');
  page.once('dialog', d => d.accept());
  await page.click('#bRestore');
  await page.waitForFunction(() => /Restored \d+ files/.test(document.getElementById('bkOut').textContent), null, {timeout: 20000});
  if (process.env.VICTOR_SHOTS_DIR) { fs.mkdirSync(process.env.VICTOR_SHOTS_DIR, {recursive: true});
   await page.screenshot({path: path.join(process.env.VICTOR_SHOTS_DIR, 'storage-health-backups-1366.png'), fullPage: true}); }
  console.log('PASS storage health, encrypted backup, wrong-passphrase refusal, verified two-step restore');
  // Brand palette: #0B0F1A background, #00E5FF primary accent, #FF2BD6 secondary accent; no light surfaces anywhere.
  assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(11, 15, 26)');
  assert.equal(await page.locator('#bDs').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(0, 229, 255)');
  assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--mag').trim().toLowerCase()), '#ff2bd6');
  for (const tab of await page.locator('#nav button').evaluateAll(bs => bs.map(b => b.dataset.t))) {
   await page.locator(`[data-t="${tab}"]`).click();
   const light = await page.evaluate(() => [...document.querySelectorAll('body *')].filter(el => el.offsetParent !== null || el.tagName === 'HEADER').map(el => {
     const m = getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || []; const [r, g, b, a = 1] = m.map(Number);
     return {el: el.tagName + '#' + el.id + '.' + el.className, r, g, b, a}; }).filter(c => c.a > 0.3 && c.r > 200 && c.g > 200 && c.b > 200).map(c => c.el));
   assert.deepEqual(light, [], `light background on ${tab}`);
  }
  console.log('PASS brand palette (#0B0F1A / #00E5FF / #FF2BD6), no light backgrounds on any tab');
  // Status dashboard: six live tiles (model, backend, API + bind, storage + drives, RAM, VRAM), no loading placeholders left.
  await page.locator('[data-t="dash"]').click();
  await page.evaluate(() => refresh());
  await page.waitForFunction(() => !document.querySelector('#tiles .skel'));
  assert.equal(await page.locator('#tiles .tile').count(), 6);
  for (const id of ['k-model', 'k-backend', 'k-api', 'k-storage', 'k-ram', 'k-vram'])
   assert.ok((await page.locator(`#${id} .v`).innerText()).trim().length > 0, id + ' empty');
  assert.match(await page.locator('#k-api .sub').innerText(), /127\.0\.0\.1:\d+ · local only/);
  assert.match(await page.locator('#k-storage .sub').innerText(), /6 drives · 3 destinations/);
  assert.match(await page.locator('#k-backend .v').innerText(), /^(CPU|Vulkan|CUDA|HIP|None)$/);
  assert.match(await page.locator('#upd').innerText(), /updated/);
  console.log('PASS status dashboard tiles: model, backend, API bind, storage drives, RAM, VRAM');
  // Keyboard: roving tabindex, arrows/Home/End switch tabs, visible cyan focus ring.
  await page.locator('#tab-dash').focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'tab-models');
  assert.equal(await page.locator('#t-models').isVisible(), true);
  await page.keyboard.press('End'); assert.equal(await page.locator('#t-logs').isVisible(), true);
  await page.keyboard.press('Home'); assert.equal(await page.locator('#t-dash').isVisible(), true);
  assert.equal(await page.locator('#nav button[tabindex="0"]').count(), 1);
  assert.equal(await page.evaluate(() => getComputedStyle(document.activeElement).outlineColor), 'rgb(0, 229, 255)');
  console.log('PASS keyboard tab navigation and focus ring');
  // Error state: a failed status request shows a banner with Retry; Retry recovers.
  await page.route('**/api/status', route => route.abort());
  await page.evaluate(() => refresh());
  await page.waitForSelector('#connErr');
  assert.equal(await page.locator('#upd').innerText(), 'offline');
  await page.unroute('**/api/status');
  await page.click('#bRetry');
  await page.waitForFunction(() => !document.querySelector('#connErr') && /updated/.test(document.querySelector('#upd').textContent));
  console.log('PASS connection error banner and retry');
  if (process.env.VICTOR_SHOTS_DIR) {
   fs.mkdirSync(process.env.VICTOR_SHOTS_DIR, {recursive: true});
   for (const [width, height] of [[1366, 900], [390, 844]]) {
    await page.setViewportSize({width, height});
    for (const tab of await page.locator('#nav button').evaluateAll(bs => bs.map(b => b.dataset.t))) {
     await page.locator(`[data-t="${tab}"]`).click(); await sleep(250);
     await page.screenshot({path: path.join(process.env.VICTOR_SHOTS_DIR, `control-center-${tab}-${width}.png`), fullPage: true});
    }
   }
  }
  if (process.env.VICTOR_STORAGE_SCREENSHOT) {
   await page.setViewportSize({width: 1366, height: 1100});
   await page.screenshot({path: process.env.VICTOR_STORAGE_SCREENSHOT, fullPage: true});
  }
  for (const width of [1366, 768, 390]) {
   await page.setViewportSize({width, height: 900});
   for (const tab of await page.locator('#nav button').evaluateAll(bs => bs.map(b => b.dataset.t))) {
    await page.locator(`[data-t="${tab}"]`).click();
    assert.equal(await page.locator(`#t-${tab}`).isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
      `horizontal page overflow: ${width}px / ${tab}`);
   }
   assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
   assert.equal(await page.locator('#nav button').first().evaluate(el => parseFloat(getComputedStyle(el).borderRadius) >= 16), true);
  }
  await page.emulateMedia({colorScheme: 'dark'});
  assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
  await page.evaluate(() => { sessionStorage.tab = 'obsolete-tab'; });
  await page.reload(); await page.waitForSelector('#t-dash.on');
  assert.deepEqual(errors, []);
  assert.deepEqual(foreign, [], 'external network requests');
  if (process.env.VICTOR_UI_SCREENSHOT) {
   await page.setViewportSize({width: 1366, height: 900});
   await page.screenshot({path: process.env.VICTOR_UI_SCREENSHOT, fullPage: true});
  }
  console.log('PASS all tabs, rounded navigation, 3 widths, dark with either host preference, stale tab recovery, no JS errors');
 } catch (e) { console.error(e.stack); process.exitCode = 1; }
 finally {
  if (browser) await browser.close();
  cc.kill();
  await new Promise(resolve => { if (cc.exitCode !== null) resolve(); else cc.once('exit', resolve); });
  fs.rmSync(tmp, {recursive: true, force: true});
 }
})();

// Chat tab: a vault written by another process (= before a reboot) unlocks, shows history, saves settings, exports, locks, clears.
const {withControlCenter, shots, sleep, assert, fs} = require('./ext_browser_lib');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const PW = 'correct horse battery';
const seed = async tmp => {
 const lib = path.resolve(__dirname, '..', 'airootfs/usr/local/lib/victorlinux');
 execFileSync('python3', ['-c', `import sys; sys.path.insert(0, ${JSON.stringify(lib)}); import chatvault as c
v = c.Vault(${JSON.stringify(tmp + '/data/chats/vault')}); v.create(${JSON.stringify(PW)}); x = v.new()
v.add_message(x["id"], "user", "Which GPU is in this PC?")
v.add_message(x["id"], "assistant", "An AMD Radeon RX 6800 XT with 16 GiB VRAM, running through Vulkan.")
y = v.new(); v.add_message(y["id"], "user", "Summarise my notes from Monday")`]);
};
(async () => {
 try {
  await withControlCenter(18791, {}, seed, async (page, tmp) => {
   await page.locator('[data-t="chats"]').click();
   await page.waitForSelector('#chUnlock:not([hidden])');
   assert.match(await page.locator('#chWarn').textContent(), /Temporary session/);
   await page.fill('#chP', 'wrong passphrase'); await page.click('#chOpen');
   await page.waitForFunction(() => /Wrong chat passphrase/.test(document.getElementById('toast').textContent));
   await page.fill('#chP', PW); await page.click('#chOpen');
   await page.waitForSelector('#chMain:not([hidden])');
   await page.waitForFunction(() => document.querySelectorAll('#chList [data-chat]').length === 2);
   await page.locator('#chList [data-chat]', {hasText: 'Which GPU'}).click();
   await page.waitForFunction(() => /RX 6800 XT/.test(document.getElementById('chLog').textContent));
   await page.fill('#chSys', 'Answer briefly.'); await page.click('#chSave');
   await page.waitForFunction(() => /Settings saved encrypted/.test(document.getElementById('toast').textContent));
   const raw = fs.readdirSync(tmp + '/data/chats/vault').map(f => fs.readFileSync(tmp + '/data/chats/vault/' + f, 'latin1')).join('');
   assert.doesNotMatch(raw, /RX 6800|Answer briefly|Monday/);
   await page.fill('#chMsg', 'hello'); await page.click('#chSend');
   await page.waitForFunction(() => /No model is running/.test(document.getElementById('toast').textContent));
   const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#chExpMd')]);
   assert.match(fs.readFileSync(await dl.path(), 'utf8'), /\*\*Assistant:\*\* An AMD Radeon/);
   await shots(page, 'chat');
   await page.click('#chLock'); await page.waitForSelector('#chUnlock:not([hidden])');
   await page.fill('#chP', PW); await page.click('#chOpen'); await page.waitForSelector('#chMain:not([hidden])');
   await page.click('#chClear');
   await page.waitForFunction(() => /Tick the confirmation/.test(document.getElementById('toast').textContent));
   await page.check('#chSure'); await page.click('#chClear');
   await page.waitForFunction(() => /Deleted 2 chat/.test(document.getElementById('toast').textContent));
  });
  console.log('PASS chat tab: unlock after restart, wrong passphrase refused, history, encrypted settings, export, lock, clear');
 } catch (e) { console.error(e.stack); process.exitCode = 1; }
})();

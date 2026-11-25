// Models tab: the "Switch model" card lists local models, marks the current one and selects another (no model running here).
const {withControlCenter, shots, assert, fs} = require('./ext_browser_lib');
const seed = async tmp => {
 fs.mkdirSync(tmp + '/data/models', {recursive: true});
 for (const n of ['qwen2.5-7b-instruct-q4_k_m.gguf', 'llama-3.2-3b-instruct-q5_k_m.gguf']) fs.writeFileSync(tmp + '/data/models/' + n, 'GGUF');
};
(async () => {
 try {
  await withControlCenter(18793, {}, seed, async page => {
   await page.locator('[data-t="models"]').click();
   await page.waitForSelector('#t-models.on #swCard:not([hidden])');
   assert.equal(await page.evaluate(() => document.getElementById('t-models').querySelector('.card').id), 'swCard');
   await page.waitForFunction(() => document.querySelectorAll('#swSel option').length === 2);
   assert.equal(await page.textContent('#swGo'), 'Select');
   await page.selectOption('#swSel', {label: 'qwen2.5-7b-instruct-q4_k_m.gguf'});
   await page.click('#swGo');
   await page.waitForFunction(() => /No model was running/.test(document.getElementById('toast').textContent));
   await page.waitForFunction(() => /qwen2\.5.*\(current\)/.test(document.getElementById('swSel').textContent));
   const bad = await page.evaluate(async () => (await api('/api/llm/switch', {model: '/etc/passwd'})).error);
   assert.match(bad, /inside a configured model directory/);
   await shots(page, 'switch-model');
  });
  console.log('browser_swap: ok');
 } catch (e) { console.error(e); process.exit(1); }
})();

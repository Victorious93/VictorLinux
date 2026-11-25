// Tune tab: saved per-PC profile renders, apply toggle persists, benchmark refuses a broken model with a clear error.
const {withControlCenter, shots, assert} = require('./ext_browser_lib');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const seed = async (tmp, env) => {
 const lib = path.resolve(__dirname, '..', 'airootfs/usr/local/lib/victorlinux');
 execFileSync('python3', ['-c', `import sys, json; sys.path.insert(0, ${JSON.stringify(lib)})
import victorcommon as tc, model_advisor, autotune
m = ${JSON.stringify(tmp + '/data/models/Qwen3-8B-Q4_K_M.gguf')}; open(m, "wb").write(b"not a gguf")
tc.save_settings(dict(tc.load_settings(), model=m))
mid = autotune.machine_id(model_advisor.cpu_info().get("name"), [g.get("pci_id") or g.get("name") for g in tc.gpus()], tc.ram_info().get("total"))
trials = [{"ngl": n, "threads": t, "tg": tg, "pp": pp} for n, t, tg, pp in [(25,4,31.0,410.2),(25,8,33.4,455.0),(25,16,32.1,460.3),(37,4,58.9,980.1),(37,8,62.7,1012.4),(37,16,60.2,1020.9)]]
autotune.Profiles(tc.data_path("config", "autotune.json")).put(mid, autotune.model_key(m), {"ngl": 37, "threads": 8, "ctx": 16384, "tg": 62.7, "pp": 1012.4, "backend": "vulkan", "device": "AMD Radeon RX 6800 XT", "trials": trials})`], {env});
};
(async () => {
 try {
  await withControlCenter(18792, {}, seed, async page => {
   await page.locator('[data-t="tune"]').click();
   await page.waitForFunction(() => /62\.7/.test(document.getElementById('tuCur').textContent));
   assert.match(await page.locator('#tuBadge').textContent(), /Tuned for this PC/);
   assert.match(await page.locator('#tuAll').textContent(), /Qwen3-8B-Q4_K_M\.gguf/);
   assert.equal(await page.isChecked('#tuApply'), true);
   await shots(page, 'tune');
   await page.uncheck('#tuApply');
   await page.waitForFunction(() => /ignored/.test(document.getElementById('toast').textContent));
   await page.reload(); await page.locator('[data-t="tune"]').click();
   await page.waitForFunction(() => document.getElementById('tuMid').textContent.length > 0);
   assert.equal(await page.isChecked('#tuApply'), false);
   await page.click('#tuRun');
   await page.waitForSelector('#toast .banner.bad');
   console.log('  refusal shown: ' + (await page.locator('#toast .banner.bad').first().textContent()).slice(0, 120));
  });
  console.log('PASS tune tab: per-PC profile, all combinations, apply toggle persists, run refused with a clear error when llama-bench cannot run');
 } catch (e) { console.error(e.stack); process.exitCode = 1; }
})();

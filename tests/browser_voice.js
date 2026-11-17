// Voice tab: off by default, explains what is missing, accepts user-supplied files, then records a (fake) microphone,
// encodes 16 kHz WAV in the page and gets it transcribed by a local stand-in for whisper.cpp.
const {withControlCenter, shots, assert, fs} = require('./ext_browser_lib');
let BIN;
const seed = async tmp => {
 const v = tmp + '/data/voice'; fs.mkdirSync(v, {recursive: true}); BIN = tmp + '/bin'; fs.mkdirSync(BIN);
 fs.writeFileSync(v + '/ggml-base.en.bin', 'x'); fs.writeFileSync(v + '/en_US-amy-medium.onnx', 'x'); fs.writeFileSync(v + '/en_US-amy-medium.onnx.json', '{}');
 fs.writeFileSync(BIN + '/whisper-cli', '#!/bin/sh\necho "Hello from the microphone."\n', {mode: 0o755});
 fs.writeFileSync(BIN + '/piper', '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --output_file ] && out=$2; shift; done\ncat >/dev/null\nprintf RIFF > "$out"\n', {mode: 0o755});
};
(async () => {
 try {
  await withControlCenter(18794, {CHROME_ARGS: '--use-fake-device-for-media-stream --use-fake-ui-for-media-stream'}, seed, async page => {
   await page.locator('[data-t="voice"]').click();
   await page.waitForFunction(() => /Off/.test(document.getElementById('voBadge').textContent));
   assert.match(await page.textContent('#voMissing'), /whisper\.cpp program/);
   await page.click('#voTalk');
   await page.waitForFunction(() => /not ready/.test(document.getElementById('toast').textContent));
   await page.check('#voOn');
   await page.selectOption('#voStt', {label: 'ggml-base.en.bin'}); await page.selectOption('#voTts', {label: 'en_US-amy-medium.onnx'});
   await page.fill('#voSttBin', BIN + '/whisper-cli'); await page.fill('#voTtsBin', BIN + '/piper');
   await page.fill('#voMax', '60'); await page.click('#voSave');
   await page.waitForFunction(() => /3 to 24 seconds/.test(document.getElementById('toast').textContent));
   await page.fill('#voMax', '8'); await page.click('#voSave');
   await page.waitForFunction(() => /Ready/.test(document.getElementById('voBadge').textContent));
   await page.click('#voTalk');
   await page.waitForFunction(() => /Stop and send/.test(document.getElementById('voTalk').textContent));
   await page.waitForTimeout(1500);
   await page.evaluate(() => scrollTo(0, 0)); await shots(page, 'voice-listening');
   await page.click('#voTalk');
   // whisper stand-in ran on the page's WAV (server validated 16 kHz mono PCM); no model is running in this test
   await page.waitForFunction(() => /No model is running/.test(document.getElementById('toast').textContent), null, {timeout: 20000});
   await page.evaluate(() => document.getElementById('toast').replaceChildren());
   await page.evaluate(() => scrollTo(0, 0)); await shots(page, 'voice');
  });
  console.log('PASS voice tab: off by default, setup validation, fake microphone → 16 kHz WAV → local whisper stand-in');
 } catch (e) { console.error(e); process.exit(1); }
})();

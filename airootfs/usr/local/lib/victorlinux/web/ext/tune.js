// Tune tab: one-time benchmark and per-PC profiles. Uses the Control Center helpers ($, api, res, toast, esc, busy).
(()=>{let timer=null;
const row=p=>`<table><tr><th>GPU layers</th><th>Threads</th><th>Context</th><th>Generation</th><th>Prompt</th></tr><tr><td>${p.ngl}</td><td>${p.threads}</td><td>${p.ctx}</td><td><b>${p.tg}</b> tok/s</td><td>${p.pp} tok/s</td></tr></table><p class="mut">${esc(p.backend)}${p.device?" · "+esc(p.device):""} · measured ${new Date(p.measured_at*1000).toLocaleString()}</p>`;
async function load(){const s=await api("/api/tune/status");if(s.error){$("tuWarn").innerHTML=`<div class="banner warn"><span>${esc(s.error)}</span></div>`;return}
 $("tuModel").textContent="Selected model: "+(s.model?s.model.split("/").pop():"none (choose one on the Models tab)");
 $("tuWarn").innerHTML=s.llm_running?`<div class="banner warn"><span>The model is running. Stop it on the Dashboard before benchmarking.</span></div>`:"";
 $("tuApply").checked=s.apply;$("tuBadge").textContent=s.current?"● Tuned for this PC":"● Not tuned";
 $("tuCur").innerHTML=s.current?row(s.current)+(s.current.trials?`<details><summary>All ${s.current.trials.length} combinations</summary><table><tr><th>GPU layers</th><th>Threads</th><th>Generation</th><th>Prompt</th></tr>${s.current.trials.map(t=>`<tr><td>${t.ngl}</td><td>${t.threads}</td><td>${t.tg}</td><td>${t.pp}</td></tr>`).join("")}</table></details>`:""):`<p class="mut">No profile yet.</p>`;
 const all=Object.entries(s.profiles||{});$("tuAll").innerHTML=all.length?`<table><tr><th>Model</th><th>GPU layers</th><th>Threads</th><th>Context</th><th>Generation</th></tr>${all.map(([k,p])=>`<tr><td>${esc(k.split(":")[0])}</td><td>${p.ngl}</td><td>${p.threads}</td><td>${p.ctx}</td><td>${p.tg} tok/s</td></tr>`).join("")}</table>`:`<p class="mut">None yet.</p>`;
 $("tuMid").textContent=s.machine?"PC fingerprint "+s.machine+" (CPU model, GPU ids and RAM size; nothing leaves this PC).":"";
 const j=s.job;$("tuBar").hidden=j.state!=="running";$("tuJob").innerHTML=j.state==="running"?`Benchmarking ${esc(j.model)} on ${esc(j.backend)}: ${j.trials} combinations (GPU layers ${j.layers.join(", ")} × threads ${j.threads.join(", ")})…`:j.state==="error"?`<span class="bad">${esc(j.error)}</span>`:j.state==="done"?`<span class="ok">Done. Fastest: ${j.profile.tg} tok/s with ${j.profile.ngl} GPU layers and ${j.profile.threads} threads.</span>`:"";
 clearTimeout(timer);if(j.state==="running")timer=setTimeout(load,3000)}
const b=(id,f)=>{const el=$(id);el.onclick=()=>busy(el,f)};
b("tuRun",async()=>{if(res(await api("/api/tune/run",{})))load()});
b("tuForget",async()=>{if(res(await api("/api/tune/forget",{})))load()});
b("tuForgetAll",async()=>{if(res(await api("/api/tune/forget",{all:true})))load()});
$("tuApply").onchange=async()=>{res(await api("/api/tune/apply",{enabled:$("tuApply").checked}));load()};
victorExt.on("tune",load);})();

// Coral tab: detected Edge TPUs, runtime/model status, settings, and a one-picture vision test. Off by default.
(()=>{
function opts(files,sel){return `<option value="">(none)</option>`+files.map(f=>`<option value="${esc(f)}"${f===sel?" selected":""}>${esc(f.split("/").pop())}</option>`).join("")}
async function load(){const j=await api("/api/coral/state");if(j.error)return;const s=j.status,c=j.config;
 $("crBadge").textContent=s.ready?"● Ready":c.enabled?"● Not ready":"● Off";$("crDir").textContent=j.folder||"coral";
 $("crDevs").innerHTML=s.devices.length?s.devices.map(d=>`<div><b>${esc(d.name)}</b> <code>${esc(d.id)}</code></div>`).join(""):"No Coral Edge TPU detected.";
 $("crWarn").innerHTML=(j.storage?"":`<div class="banner warn"><span>Set up storage first (Storage tab).</span></div>`)+`<div class="banner ok"><span>${esc(s.llm_note)}</span></div>`;
 $("crMissing").innerHTML=s.missing.map(m=>`<div class="banner warn"><span>${esc(m)}</span></div>`).join("");
 $("crOn").checked=c.enabled;$("crVision").innerHTML=opts(s.edgetpu_files,c.vision_model);$("crWake").innerHTML=opts(s.edgetpu_files,c.wake_model);
 $("crLabel").value=c.wake_label;$("crThr").value=c.wake_threshold}
$("crSave").onclick=()=>busy($("crSave"),async()=>{if(res(await api("/api/coral/config",{enabled:$("crOn").checked,vision_model:$("crVision").value,wake_model:$("crWake").value,wake_label:$("crLabel").value,wake_threshold:+$("crThr").value})))load()});
$("crTry").onclick=()=>busy($("crTry"),async()=>{const f=$("crImg").files[0];if(!f){toast("Pick a picture first","warn");return}
 const b64=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(String(fr.result).split(",")[1]);fr.readAsDataURL(f)});
 const j=await api("/api/coral/classify",{image:b64});if(j.error){toast(j.error,"bad");return}
 $("crOut").innerHTML=j.top.map(t=>`<div>${esc(t.label)} <span class="mut">${(t.score*100).toFixed(1)}%</span></div>`).join("")});
victorExt.on("coral",load);})();

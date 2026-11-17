// Model switch card, shown at the top of the Models tab. Uses the Control Center helpers ($, api, res, toast, esc, busy).
(()=>{const card=$("swCard"),sec=$("t-models");sec.insertBefore(card,sec.querySelector(".card"));card.hidden=false;let timer=null;
async function load(){const j=await api("/api/llm/switch");if(j.error)return;const cur=$("swSel").value;
 $("swSel").innerHTML=j.models.map(m=>`<option value="${esc(m.path)}"${m.path===j.model?" selected":""}>${esc(m.name)}${m.path===j.model?" (current)":""}</option>`).join("")||`<option value="">No models found</option>`;
 if(cur&&j.models.some(m=>m.path===cur))$("swSel").value=cur;
 const s=j.swap;$("swGo").textContent=j.running?"Switch now":"Select";
 $("swOut").innerHTML=s.state==="switching"?`<div class="banner warn"><span>Switching ${esc(s.frm)} → <b>${esc(s.to)}</b>… API requests are being held.</span></div>`:
  s.state==="done"?`<div class="banner ok"><span>${esc(s.message)} (${s.seconds}s)</span></div>`:
  s.state==="error"?`<div class="banner bad"><span>${esc(s.message)} Reason: ${esc(s.error)}</span></div>`:"";
 clearTimeout(timer);if(s.state==="switching")timer=setTimeout(load,1500)}
$("swGo").onclick=()=>busy($("swGo"),async()=>{if(res(await api("/api/llm/switch",{model:$("swSel").value})))load()});
victorExt.on("models",load);})();

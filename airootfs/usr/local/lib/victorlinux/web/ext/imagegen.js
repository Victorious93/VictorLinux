// Images tab: settings, start a generation, poll progress, show the finished PNG. Off by default; nothing downloaded.
(()=>{let S=null,T=null;const sizes=[256,320,384,448,512,576,640,704,768,832,896,960,1024];
$("igW").innerHTML=$("igH").innerHTML=sizes.map(s=>`<option${s===512?" selected":""}>${s}</option>`).join("");
function bar(j){const p=j.steps?Math.round(100*j.step/j.steps):0;$("igBar").firstElementChild.style.width=(j.state==="done"?100:p)+"%"}
async function poll(){const j=await api("/api/imagegen/job");if(j.error)return;bar(j);
 const run=j.state==="running"||j.state==="cancelling";$("igGo").disabled=run||!(S&&S.status.ready);$("igCancel").disabled=!run;
 if(run){$("igMsg").textContent=`Drawing… step ${j.step} of ${j.steps}`;T=setTimeout(poll,1500);return}
 T=null;if(j.state==="done"&&j.png){$("igOut").innerHTML=`<img alt="Generated picture" src="data:image/png;base64,${j.png}"><p class="mut">Saved as <code>${esc(j.file)}</code></p>`;$("igMsg").textContent="Done."}
 else if(j.state==="error"||j.state==="cancelled")$("igMsg").textContent=j.state==="cancelled"?"Cancelled.":j.error}
async function load(){const j=await api("/api/imagegen/state");if(j.error)return;S=j;const st=j.status;
 $("igBadge").textContent=st.ready?"● Ready":st.enabled?"● Not ready":"● Off";$("igDir").textContent=j.folder||"images/models";
 $("igWarn").innerHTML=j.storage?"":`<div class="banner warn"><span>Set up storage first (Storage tab); settings and pictures live on the data partition.</span></div>`;
 $("igMissing").innerHTML=st.missing.map(m=>`<div class="banner warn"><span>${esc(m)}</span></div>`).join("");
 $("igOn").checked=j.config.enabled;$("igBin").value=j.config.bin;
 $("igModel").innerHTML=`<option value="">(none)</option>`+st.files.map(f=>`<option value="${esc(f)}"${f===j.config.model?" selected":""}>${esc(f.split("/").pop())}</option>`).join("");
 if(!T)poll()}
$("igSave").onclick=()=>busy($("igSave"),async()=>{if(res(await api("/api/imagegen/config",{enabled:$("igOn").checked,model:$("igModel").value,bin:$("igBin").value.trim()})))load()});
$("igGo").onclick=()=>busy($("igGo"),async()=>{const j=await api("/api/imagegen/generate",{prompt:$("igPrompt").value,negative:$("igNeg").value,width:+$("igW").value,height:+$("igH").value,steps:+$("igSteps").value,cfg:+$("igCfg").value,seed:+$("igSeed").value});
 if(j.error){toast(j.error,"bad");return}$("igOut").innerHTML="";poll()});
$("igCancel").onclick=async()=>{res(await api("/api/imagegen/cancel",{}));poll()};
victorExt.on("imagegen",load);})();

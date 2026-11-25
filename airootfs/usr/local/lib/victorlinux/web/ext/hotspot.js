// Hotspot tab: settings, start/stop via the Control Center (victor-priv does the root work), Wi-Fi QR on request.
(()=>{let H=null;const CH={"2.4":[1,2,3,4,5,6,7,8,9,10,11,12,13],"5":[36,40,44,48]};
function chans(sel){const b=$("hsBand").value;$("hsCh").innerHTML=CH[b].map(c=>`<option${c===sel?" selected":""}>${c}</option>`).join("")}
async function load(){const j=await api("/api/hotspot/state");if(j.error)return;H=j;const c=j.config;
 $("hsBadge").textContent=j.running?`● On · ${j.clients} device${j.clients===1?"":"s"}`:"● Off";
 const w=[];if(!j.storage)w.push(["warn","Set up storage first (Storage tab); hotspot settings live on the data partition."]);
 if(!j.radios.ifaces.length)w.push(["bad","No Wi-Fi adapter found on this PC."]);else if(!j.radios.ap_capable)w.push(["warn","This Wi-Fi adapter does not report access-point (AP) mode; the hotspot may not start."]);
 if(!j.api_ready)w.push(["warn",j.api_why]);
 $("hsWarn").innerHTML=w.map(x=>`<div class="banner ${x[0]}"><span>${esc(x[1])}</span></div>`).join("");
 if(document.activeElement.tagName!=="INPUT"){$("hsSsid").value=c.ssid;$("hsBand").value=c.band;chans(c.channel);$("hsCc").value=c.country}
 $("hsIf").innerHTML=`<option value="">automatic</option>`+j.radios.ifaces.map(i=>`<option${i===c.iface?" selected":""}>${esc(i)}</option>`).join("");
 $("hsInfo").innerHTML=`This PC is <b>${esc(j.address)}</b> on the hotspot. API: <code>${esc(j.api_url)}</code>. Nothing is forwarded; phones keep their own mobile data for the internet.`;
 $("hsStart").disabled=j.running||!j.api_ready;$("hsStop").disabled=!j.running}
function body(extra){return Object.assign({ssid:$("hsSsid").value,psk:$("hsPsk").value,band:$("hsBand").value,channel:+$("hsCh").value,country:$("hsCc").value.toUpperCase(),iface:$("hsIf").value},extra||{})}
async function reveal(){const j=await api("/api/hotspot/reveal",{});if(j.error){toast(j.error,"bad");return}$("hsQr").innerHTML=`<div class="qr">${j.qr_svg}</div>`+`<div><div class="mut">Network</div><b>${esc(j.ssid)}</b><div class="mut" style="margin-top:8px">Password</div><code>${esc(j.psk)}</code></div>`;$("hsJoin").hidden=false}
$("hsBand").onchange=()=>chans(+$("hsCh").value);
$("hsSave").onclick=()=>busy($("hsSave"),async()=>{if(res(await api("/api/hotspot/config",body()))){$("hsPsk").value="";load()}});
$("hsNew").onclick=()=>busy($("hsNew"),async()=>{if(res(await api("/api/hotspot/config",body({new_password:true})),"New password saved.")){$("hsPsk").value="";load();if(!$("hsJoin").hidden)reveal()}});
$("hsStart").onclick=()=>busy($("hsStart"),async()=>{if(res(await api("/api/hotspot/start",{}))){load();reveal()}});
$("hsStop").onclick=()=>busy($("hsStop"),async()=>{res(await api("/api/hotspot/stop",{}));load()});
$("hsHide").onclick=()=>{$("hsQr").innerHTML="";$("hsJoin").hidden=true};
victorExt.on("hotspot",load);})();

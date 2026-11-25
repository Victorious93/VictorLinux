// Chat tab: encrypted persistent chats. Uses the Control Center helpers ($, api, res, toast, esc, busy).
(()=>{let cur=null;
const b=(id,f)=>{const el=$(id);el.onclick=()=>busy(el,f)};
function draw(c){cur=c;$("chTitle").textContent=c?c.title:"New chat";$("chLog").innerHTML=c&&c.messages.length?c.messages.map(m=>`<div class="msg ${m.role==="user"?"user":"assistant"}">${esc(m.content)}</div>`).join(""):`<p class="mut">No messages yet.</p>`;$("chLog").scrollTop=1e9}
async function list(){const j=await api("/api/chats/list");if(j.error)return;$("chList").innerHTML=j.chats.length?j.chats.map(c=>`<button class="b s" data-chat="${esc(c.id)}">${esc(c.title)} <span class="mut">(${c.messages})</span></button>`).join(""):`<p class="mut">No saved chats.</p>`;if(j.damaged)toast(j.damaged+" chat file(s) could not be opened","warn")}
async function load(){const s=await api("/api/chats/state");if(s.error){$("chWarn").innerHTML=`<div class="banner warn"><span>${esc(s.error)}</span></div>`;return}
 $("chWarn").innerHTML=(!s.available?`<div class="banner bad"><span>Encryption library missing: chats cannot be saved. Nothing is stored unencrypted.</span></div>`:"")+(s.warning?`<div class="banner warn"><span>${esc(s.warning)}</span></div>`:"")+(!s.model_running?`<div class="banner warn"><span>No model is running. You can read saved chats; start a model on the Dashboard to send messages.</span></div>`:"");
 $("chSetup").hidden=!s.available||s.exists;$("chUnlock").hidden=!s.exists||s.unlocked;$("chMain").hidden=!s.unlocked;$("chBadge").textContent=s.unlocked?"● Unlocked":"● Locked";
 if(s.unlocked){const t=s.settings;$("chSys").value=t.system_prompt;$("chTemp").value=t.temperature;$("chMax").value=t.max_tokens;$("chAuto").value=t.auto_lock_minutes;await list();if(!cur)draw(null)}}
$("chList").addEventListener("click",async e=>{const x=e.target.closest("[data-chat]");if(!x)return;const c=await api("/api/chats/get?id="+encodeURIComponent(x.dataset.chat));if(!c.error)draw(c);else toast(c.error,"bad")});
b("chCreate",async()=>{const j=await api("/api/chats/setup",{passphrase:$("chP1").value,confirm:$("chP2").value});if(res(j,"Chat vault created")){$("chP1").value=$("chP2").value="";load()}});
b("chOpen",async()=>{const j=await api("/api/chats/unlock",{passphrase:$("chP").value});$("chP").value="";if(res(j,"Chats unlocked"))load()});
b("chLock",async()=>{res(await api("/api/chats/lock",{}));cur=null;load()});
b("chNew",async()=>{const c=await api("/api/chats/new",{});if(res(c,"New chat")){draw(c);list()}});
b("chSend",async()=>{const m=$("chMsg").value;const c=await api("/api/chats/send",{id:cur?cur.id:"",message:m});if(c.error){toast(c.error,"bad");if(/locked/i.test(c.error))load();return}$("chMsg").value="";draw(c);list()});
b("chDel",async()=>{if(!cur)return;if(res(await api("/api/chats/delete",{id:cur.id}),"Chat deleted")){draw(null);list()}});
b("chSave",async()=>res(await api("/api/chats/settings",{settings:{system_prompt:$("chSys").value,temperature:+$("chTemp").value,max_tokens:+$("chMax").value,auto_lock_minutes:+$("chAuto").value}}),"Settings saved encrypted"));
async function exp(fmt){const j=await api("/api/chats/export?fmt="+fmt);if(j.error){toast(j.error,"bad");return}const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([j.text],{type:"text/plain"}));a.download=j.filename;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000);toast(j.warning,"warn")}
b("chExpMd",()=>exp("md"));b("chExpJson",()=>exp("json"));
b("chClear",async()=>{const j=await api("/api/chats/clear",{confirm:$("chSure").checked,forget_vault:$("chForget").checked});if(res(j,`Deleted ${j.deleted_chats} chat(s)`)){$("chSure").checked=$("chForget").checked=false;cur=null;draw(null);load()}});
victorExt.on("chats",load);})();

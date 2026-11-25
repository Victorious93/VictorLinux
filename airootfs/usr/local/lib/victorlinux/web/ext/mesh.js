// Mesh tab (Victorious Mesh): join your own Headscale, toggles, share password, peers. Root work is victor-mesh-priv.
(()=>{
async function loadMesh(){const m=await api("/api/mesh",{action:"status"});if(m.error){$("meshState").textContent=m.error;return}
 $("meshTag").textContent=m.tag||m.edition_tag||"tag:victorlinux";
 const st=!m.installed?"tailscale is not installed in this image.":m.backend==="Running"?"Connected as "+(m.hostname||"?")+" · "+m.mesh_ips.join(", ")+" · "+m.control_url:m.joined?"Joined "+m.control_url+", not connected.":"Not joined.";
 $("meshState").textContent=st+(m.joined&&!m.state_persistent?" No persistent storage: you must join again after a reboot.":"");
 if(m.control_url&&!$("meshUrl").value)$("meshUrl").value=m.control_url;
 document.querySelectorAll(".meshT").forEach(c=>{c.checked=!!(m.toggles||{})[c.dataset.s];c.disabled=!m.joined});
 $("meshFiles").innerHTML=(m.share_files||[]).length?"<tr><th>Shared file</th><th>Size</th><th></th></tr>"+m.share_files.map(f=>`<tr><td>${esc(f.name)}</td><td>${(f.size/2**20).toFixed(1)} MiB</td><td><button class="b s" data-meshrm="${esc(f.name)}">Stop sharing</button></td></tr>`).join(""):'<tr><td class="mut">Nothing shared.'+(m.share_url?" Page: "+esc(m.share_url):"")+'</td></tr>';
 $("meshPeers").innerHTML=(m.peers||[]).length?"<tr><th>Name</th><th>Address</th><th>State</th></tr>"+m.peers.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc((p.ips||[])[0]||"")}</td><td>${p.online?"online":"offline"}</td></tr>`).join(""):'<tr><td class="mut">No peers.</td></tr>'}
function meshPw(j){if(j&&j.password){$("meshPw").innerHTML=`<div class="banner warn">Share password (shown once, any user name works): <code>${esc(j.password)}</code></div>`}}
async function meshAct(body,okm){const j=await api("/api/mesh",body);if(res(j,okm||(j.notes||[]).join(" · ")||undefined))meshPw(j);loadMesh();return j}
$("bMeshJoin").onclick=async()=>{const j=await meshAct({action:"join",url:$("meshUrl").value,key:$("meshKey").value,name:$("meshName").value},"Joined the mesh");$("meshKey").value=""};
$("bMeshUp").onclick=()=>meshAct({action:"up"},"Connected");$("bMeshDown").onclick=()=>meshAct({action:"down"});$("bMeshRef").onclick=()=>loadMesh();
$("bMeshLeave").onclick=()=>{if(confirm("Leave the mesh? This PC's mesh identity and settings are removed."))meshAct({action:"leave"})};
$("bMeshPw").onclick=()=>meshAct({action:"share-password"},"New share password made");
document.querySelectorAll(".meshT").forEach(c=>c.onchange=()=>meshAct({action:"set",toggle:c.dataset.k,on:c.checked}));
$("meshFiles").onclick=e=>{const b=e.target.closest("button[data-meshrm]");if(b)meshAct({action:"share-remove",name:b.dataset.meshrm},"Stopped sharing")};
victorExt.on("mesh",loadMesh);
})();

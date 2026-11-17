// Voice tab: records 16 kHz mono WAV with Web Audio (no codecs, no network), posts it to /api/voice/ask on 127.0.0.1,
// shows the transcript and reply, and plays the Piper answer through Web Audio (CSP stays 'self').
(()=>{let V=null,rec=null,hist=[],lastWake=null,poll=null;
const RATE=16000;
function wav(samples,rate){const n=Math.floor(samples.length*RATE/rate),pcm=new Int16Array(n),k=rate/RATE;
 for(let i=0;i<n;i++){let a=0,c=0;for(let j=Math.floor(i*k);j<Math.min(samples.length,Math.floor((i+1)*k));j++){a+=samples[j];c++}const v=Math.max(-1,Math.min(1,c?a/c:0));pcm[i]=v<0?v*32768:v*32767}
 const b=new ArrayBuffer(44+pcm.length*2),d=new DataView(b),w=(o,s)=>[...s].forEach((ch,i)=>d.setUint8(o+i,ch.charCodeAt(0)));
 w(0,"RIFF");d.setUint32(4,36+pcm.length*2,true);w(8,"WAVE");w(12,"fmt ");d.setUint32(16,16,true);d.setUint16(20,1,true);d.setUint16(22,1,true);
 d.setUint32(24,RATE,true);d.setUint32(28,RATE*2,true);d.setUint16(32,2,true);d.setUint16(34,16,true);w(36,"data");d.setUint32(40,pcm.length*2,true);
 new Int16Array(b,44).set(pcm);return new Uint8Array(b)}
function b64(u8){let s="";for(let i=0;i<u8.length;i+=32768)s+=String.fromCharCode.apply(null,u8.subarray(i,i+32768));return btoa(s)}
function add(role,text){hist.push({role,content:text});hist=hist.slice(-12);const d=document.createElement("div");d.className="msg "+role;d.textContent=text;$("voLog").appendChild(d);$("voLog").scrollTop=1e9}
async function play(a){try{const raw=Uint8Array.from(atob(a),c=>c.charCodeAt(0)).buffer,ctx=new AudioContext(),buf=await ctx.decodeAudioData(raw),s=ctx.createBufferSource();s.buffer=buf;s.connect(ctx.destination);s.onended=()=>ctx.close();s.start()}catch(e){toast("Could not play the reply audio.","bad")}}
async function start(){if(rec)return;if(!V||!V.status.ready){toast("Voice chat is not ready. See Setup below.","bad");return}
 let stream;try{stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}})}catch(e){toast("Microphone not available or permission denied.","bad");return}
 const ctx=new AudioContext(),src=ctx.createMediaStreamSource(stream),node=ctx.createScriptProcessor(4096,1,1),chunks=[];
 node.onaudioprocess=e=>{const x=e.inputBuffer.getChannelData(0);chunks.push(new Float32Array(x));let p=0;for(const v of x)p=Math.max(p,Math.abs(v));$("voMeter").firstChild.style.width=Math.min(100,p*140)+"%"};
 src.connect(node);node.connect(ctx.destination);
 rec={stream,ctx,node,chunks,timer:setTimeout(stop,V.config.max_seconds*1000)};
 $("voTalk").classList.add("rec");$("voTalk").textContent="■ Stop and send";$("voHint").textContent=`Listening… (up to ${V.config.max_seconds} s)`}
async function stop(){if(!rec)return;const r=rec;rec=null;clearTimeout(r.timer);r.node.disconnect();r.stream.getTracks().forEach(t=>t.stop());const rate=r.ctx.sampleRate;await r.ctx.close();
 $("voTalk").classList.remove("rec");$("voTalk").textContent="● Start talking";$("voMeter").firstChild.style.width="0";
 const all=new Float32Array(r.chunks.reduce((a,c)=>a+c.length,0));let o=0;for(const c of r.chunks){all.set(c,o);o+=c.length}
 $("voHint").textContent="Transcribing and thinking…";
 const j=await api("/api/voice/ask",{audio:b64(wav(all,rate)),history:hist});$("voHint").textContent="Press the button, speak, press again to send.";
 if(j.error){toast(j.error,"bad");return}add("user",j.text);if(j.reply)add("assistant",j.reply);if(j.audio)play(j.audio);if(j.tts_error)toast(j.tts_error,"bad")}
function opt(list,cur){return `<option value="">(none)</option>`+list.map(p=>`<option value="${esc(p)}"${p===cur?" selected":""}>${esc(p.split("/").pop())}</option>`).join("")}
async function load(){const j=await api("/api/voice/state");if(j.error)return;V=j;const c=j.config,s=j.status;
 $("voBadge").textContent=s.ready?"● Ready":c.enabled?"● Not set up":"● Off";$("voDir").textContent=j.voice_dir||"(set up Storage first)";
 $("voWarn").innerHTML=!j.storage?`<div class="banner warn"><span>Set up storage first (Storage tab); voice settings and model files live on the data partition.</span></div>`:"";
 $("voMissing").innerHTML=s.missing.length?`<div class="banner warn"><span>${s.missing.map(esc).join(" · ")}</span></div>`:`<div class="banner ok"><span>Speech-to-text ${s.stt_ready?"ready":"not ready"} · voice ${s.tts_ready?"ready":"text only"}</span></div>`;
 $("voOn").checked=c.enabled;$("voSpeak").checked=c.speak_replies;$("voStt").innerHTML=opt(s.files.stt,c.stt_model);$("voTts").innerHTML=opt(s.files.tts,c.tts_model);
 $("voLang").value=c.language;$("voMax").value=c.max_seconds;$("voSttBin").value=c.stt_bin;$("voTtsBin").value=c.tts_bin;
 if(lastWake===null)lastWake=j.wake_seq;else if(j.wake_seq>lastWake){lastWake=j.wake_seq;if($("voHands").checked)start()}}
$("voTalk").onclick=()=>rec?stop():start();
$("voSave").onclick=()=>busy($("voSave"),async()=>{if(res(await api("/api/voice/config",{enabled:$("voOn").checked,speak_replies:$("voSpeak").checked,stt_model:$("voStt").value,tts_model:$("voTts").value,language:$("voLang").value,max_seconds:+$("voMax").value,stt_bin:$("voSttBin").value,tts_bin:$("voTtsBin").value})))load()});
$("voTest").onclick=()=>busy($("voTest"),async()=>{const j=await api("/api/voice/say",{});if(j.error)toast(j.error,"bad");else play(j.audio)});
$("voHands").onchange=()=>{clearInterval(poll);if($("voHands").checked)poll=setInterval(()=>{if($("t-voice").classList.contains("on"))load()},2000)};
victorExt.on("voice",load);})();

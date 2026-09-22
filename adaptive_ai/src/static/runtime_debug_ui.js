// Opt-in bounded runtime trace controls for Diagnostics.
(()=>{
  if(window.__runtimeDebugUiInstalled)return;

  const root=()=>document.getElementById('runtimeDebugPanel');
  const ms=value=>value==null?'—':`${Number(value).toLocaleString(undefined,{maximumFractionDigits:1})} ms`;
  const escLocal=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fieldSummary=fields=>{
    const entries=Object.entries(fields||{}).slice(0,5);
    return entries.map(([k,v])=>`${escLocal(k)}=${escLocal(Array.isArray(v)?v.join(','):v)}`).join(' · ');
  };

  async function request(path,options={}){
    const response=await fetch(path,{
      cache:'no-store',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      ...options,
    });
    let body={};
    try{body=await response.json();}catch(_e){}
    if(!response.ok)throw Error(body.error||`HTTP ${response.status}`);
    return body;
  }

  function render(status={}){
    const node=root();if(!node)return;
    const dbg=status.runtime_debug||{};
    const metric=dbg.event_to_intent||status.telemetry?.metrics?.event_to_intent||{};
    const enabled=Boolean(dbg.enabled);
    const active=Array.isArray(dbg.active)?dbg.active:[];
    const heavy=status.heavy_job||dbg.heavy_job||null;
    const activity=active.length
      ?active.map(item=>`<div class="runtime-debug-active"><b>${escLocal(item.operation||'work')}</b><span>${escLocal(item.thread||'')} · ${ms(item.age_ms)}${fieldSummary(item.fields)?' · '+fieldSummary(item.fields):''}</span></div>`).join('')
      :`<div class="empty">${heavy?`Heavy job: ${escLocal(heavy)}`:'No traced operation is active right now.'}</div>`;
    node.innerHTML=`
      <div class="section-title runtime-debug-title">
        <div>
          <h2>Runtime debug log</h2>
          <span>Opt-in RAM trace · event → intent latency + current execution</span>
        </div>
        <div class="runtime-debug-controls">
          <button class="${enabled?'danger':'primary'}" id="runtimeDebugToggle">${enabled?'Stop debug log':'Start debug log'}</button>
          <button class="ghost" id="runtimeDebugDownload" ${Number(dbg.entries||0)<=0&&!active.length?'disabled':''}>Download log</button>
        </div>
      </div>
      <div class="history-grid runtime-debug-grid">
        <div><b>${ms(metric.recent_p95_ms)}</b><span>event → intent p95 · last 60 s</span></div>
        <div><b>${ms(metric.p95_ms)}</b><span>event → intent p95 · retained telemetry</span></div>
        <div><b>${enabled?'ON':'OFF'}</b><span>debug logging</span></div>
        <div><b>${Number(dbg.entries||0).toLocaleString()}</b><span>trace rows in RAM · max ${Number(dbg.capacity||4096).toLocaleString()}</span></div>
      </div>
      <div class="runtime-debug-current">
        <div class="history-timing"><b>Currently executing</b><span>${heavy?`Heavy job: ${escLocal(heavy)}`:'Heavy job: idle'} · ${active.length} traced span${active.length===1?'':'s'}</span></div>
        ${activity}
      </div>
      <div class="history-meta">
        <span>${Number(metric.recent_count||0)} event→intent samples in the recent window</span>
        <span>${Number(dbg.dropped_entries||0)} overwritten trace rows</span>
        <span>Logging is disabled by default and does not write to disk.</span>
      </div>`;
    const toggle=document.getElementById('runtimeDebugToggle');
    const download=document.getElementById('runtimeDebugDownload');
    if(toggle)toggle.onclick=()=>window.toggleRuntimeDebugLog?.(toggle,!enabled);
    if(download)download.onclick=()=>window.downloadRuntimeDebugLog?.(download);
  }

  window.toggleRuntimeDebugLog=async(button,enabled)=>{
    if(button?.dataset?.busy==='1')return;
    const original=button?.textContent||'';
    if(button){button.dataset.busy='1';button.disabled=true;button.textContent=enabled?'Starting…':'Stopping…';}
    try{
      const result=await request('api/debug/runtime-log',{
        method:'POST',
        body:JSON.stringify({enabled:Boolean(enabled),clear:Boolean(enabled)}),
      });
      const status={...(typeof lastStatus==='object'?lastStatus:{}),runtime_debug:result,heavy_job:result.heavy_job};
      render(status);
      try{await window.load?.();}catch(_e){}
    }catch(error){
      alert('Runtime debug log: '+(error?.message||String(error)));
      if(button&&button.isConnected){button.disabled=false;button.textContent=original;}
    }finally{
      if(button&&button.isConnected)delete button.dataset.busy;
    }
  };

  window.downloadRuntimeDebugLog=button=>{
    if(button?.dataset?.busy==='1')return;
    if(button){button.dataset.busy='1';button.disabled=true;button.textContent='Preparing…';}
    const anchor=document.createElement('a');
    anchor.href='api/debug/runtime-log/download';
    anchor.download='adaptive-ai-runtime-debug.json';
    anchor.style.display='none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    if(button){
      button.textContent='Downloaded ✓';
      setTimeout(()=>{
        if(button.isConnected){button.disabled=false;button.textContent='Download log';delete button.dataset.busy;}
      },1500);
    }
  };

  window.renderRuntimeDebugDiagnostics=render;
  window.__runtimeDebugUiInstalled=true;
  try{if(typeof lastStatus==='object')render(lastStatus);}catch(_e){}
})();

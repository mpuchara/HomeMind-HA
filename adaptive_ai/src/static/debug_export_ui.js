// One-click bounded Correct learning diagnostic export for Live and Candidate cards.
(()=>{
  if(window.exportCorrectLearningDebug)return;

  const safePart=value=>String(value??'')
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g,'-')
    .replace(/^-+|-+$/g,'')
    .slice(0,80)||'agent';

  const stamp=()=>{
    const d=new Date();
    const p=n=>String(n).padStart(2,'0');
    return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  };

  async function parseResponse(response){
    const text=await response.text();
    let body=null;
    try{body=JSON.parse(text);}catch(_){}
    if(!response.ok){
      const message=body?.error||text||`HTTP ${response.status}`;
      throw Error(message.slice(0,1200));
    }
    if(!body||typeof body!=='object')throw Error('Debug endpoint did not return JSON');
    return body;
  }

  function downloadJson(payload, ref){
    const root=payload.root_agent||{};
    const identity=root.name||root.target_entity||payload.root_agent_id||ref;
    const filename=`correct-learning-${safePart(identity)}-${stamp()}.json`;
    const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
    const url=URL.createObjectURL(blob);
    const anchor=document.createElement('a');
    anchor.href=url;
    anchor.download=filename;
    anchor.style.display='none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(()=>URL.revokeObjectURL(url),5000);
    return filename;
  }

  window.exportCorrectLearningDebug=async(ref,button)=>{
    ref=String(ref||'').trim();
    if(!ref){alert('Debug export: missing agent/generation reference');return null;}
    if(button?.dataset?.debugExportBusy==='1')return null;

    const original=button?.textContent||'Export debug';
    if(button){
      button.dataset.debugExportBusy='1';
      button.disabled=true;
      button.textContent='Exporting…';
    }
    try{
      const query=new URLSearchParams({
        detail:'full',
        label_limit:'256',
        window_seconds:'120',
        raw_rows_per_label:'768',
      });
      const response=await fetch(
        `api/agents/${encodeURIComponent(ref)}/debug/correct-learning?${query.toString()}`,
        {cache:'no-store',headers:{'Accept':'application/json'}}
      );
      const payload=await parseResponse(response);
      const filename=downloadJson(payload,ref);
      if(button){
        button.textContent='Downloaded ✓';
        setTimeout(()=>{if(button.isConnected)button.textContent=original;},1800);
      }
      return {filename,payload};
    }catch(error){
      console.error('Correct learning debug export failed',error);
      alert('Debug export failed: '+(error?.message||String(error)));
      if(button)button.textContent=original;
      return null;
    }finally{
      if(button){
        delete button.dataset.debugExportBusy;
        button.disabled=false;
      }
    }
  };
})();

// 0.14.16 shared GET broker for Raspberry Pi class hosts.
// Several legacy UI layers still own independent display refresh loops. Rather than let
// those loops issue the same HTTP reads in parallel, coalesce/cache only the known
// read-only hot endpoints. Mutations are never intercepted.
(()=>{
  if(window.__adaptiveAiPollingGuard)return;
  const upstreamFetch=window.fetch.bind(window);
  const inflight=new Map(),cache=new Map();
  const ttlFor=path=>{
    const base=path.split('?')[0].replace(/^\.?\//,'');
    if(base.endsWith('api/live'))return 900;
    if(base.endsWith('api/candidate-live'))return 900;
    if(base.endsWith('api/candidates'))return 1800;
    if(base.endsWith('api/agents'))return 3000;
    if(base.endsWith('api/status'))return 1800;
    if(base.endsWith('api/events'))return 3000;
    return 0;
  };
  const keyFor=input=>{
    const raw=typeof input==='string'?input:(input?.url||'');
    try{
      const u=new URL(raw,window.location.href);
      const path=u.pathname.replace(/^.*\/api\//,'api/');
      return path+(u.search||'');
    }catch(_e){return String(raw).replace(/^\.\//,'');}
  };
  const snapshot=async response=>({
    body:await response.text(),status:response.status,statusText:response.statusText,
    headers:[...response.headers.entries()],ok:response.ok,
  });
  const responseFrom=x=>new Response(x.body,{status:x.status,statusText:x.statusText,headers:x.headers});
  window.fetch=(input,init={})=>{
    const method=String(init?.method||'GET').toUpperCase();
    if(method!=='GET'&&method!=='HEAD')return upstreamFetch(input,init);
    const key=keyFor(input),ttl=ttlFor(key);
    if(!ttl)return upstreamFetch(input,init);
    const now=performance.now(),cached=cache.get(key);
    if(cached&&now-cached.at<ttl)return Promise.resolve(responseFrom(cached.value));
    if(inflight.has(key))return inflight.get(key).then(responseFrom);
    // A caller-specific AbortSignal must not cancel a read shared by other UI layers.
    // The upstream home.js read guard still provides the bounded 12 s safety timeout.
    const sharedInit={...init};
    delete sharedInit.signal;
    const request=upstreamFetch(input,sharedInit).then(snapshot).then(value=>{
      if(value.ok)cache.set(key,{at:performance.now(),value});
      return value;
    }).finally(()=>inflight.delete(key));
    inflight.set(key,request);
    return request.then(responseFrom);
  };
  window.__adaptiveAiPollingGuard={
    version:1,
    clear:()=>cache.clear(),
    snapshot:()=>({cached:[...cache.keys()],inflight:[...inflight.keys()]}),
  };
})();

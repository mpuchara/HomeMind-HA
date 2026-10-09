// Start card reads only after all classic script layers have installed their renderers.
// This is independent of backend readiness: /api/live can still bootstrap while status waits.
(()=>{
  const pending=[];
  window.__adaptiveAiUiReady=false;
  window.whenAdaptiveUiReady=start=>{
    if(window.__adaptiveAiUiReady)queueMicrotask(start);
    else pending.push(start);
  };
  const ready=()=>{
    if(window.__adaptiveAiUiReady)return;
    window.__adaptiveAiUiReady=true;
    for(const start of pending.splice(0))queueMicrotask(start);
  };
  if(document.readyState==='complete')ready();
  else{
    document.addEventListener('DOMContentLoaded',ready,{once:true});
    // Also supports loading this helper after DOMContentLoaded but before window.load.
    window.addEventListener('load',ready,{once:true});
  }
})();

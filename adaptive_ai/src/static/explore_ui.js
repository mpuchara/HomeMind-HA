// Final generation-card binding for Explore. Loaded after both Live and Candidate renderers.
(()=>{
  function bind(){
    document.querySelectorAll('#agents [data-wf="explore"]').forEach(button=>{
      const card=button.closest('.agent');
      if(!card)return;
      const ref=card.classList.contains('candidate-agent')
        ? card.dataset.generationId
        : card.dataset.agentId;
      if(!ref)return;
      button.disabled=false;
      button.removeAttribute('title');
      button.dataset.exploreReady='1';
      button.onclick=()=>window.openExplore?.(ref);
    });
  }
  const root=document.getElementById('agents');
  if(root){
    bind();
    new MutationObserver(bind).observe(root,{childList:true,subtree:true});
  }
  window.bindExploreButtons=bind;
})();

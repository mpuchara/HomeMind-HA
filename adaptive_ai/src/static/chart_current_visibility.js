// Keep the physical Current trace visually authoritative in the Correct chart.
// This is UI-only: it never changes history, Candidate learning, scoring, or control.
(()=>{
  const STYLE={outline:'#04111f',current:'#73dbec'};

  function enhance(svg){
    if(!(svg instanceof SVGElement))return;
    const current=svg.querySelector('path[data-series="current"]');
    if(!current)return;

    let outline=svg.querySelector('path[data-series="current-outline"]');
    if(!outline){
      outline=current.cloneNode(false);
      outline.dataset.series='current-outline';
      current.parentNode.insertBefore(outline,current);
    }
    outline.setAttribute('d',current.getAttribute('d')||'');
    outline.setAttribute('fill','none');
    outline.setAttribute('stroke',STYLE.outline);
    outline.setAttribute('stroke-width','6');
    outline.setAttribute('stroke-linejoin','round');
    outline.setAttribute('stroke-linecap','round');
    outline.setAttribute('opacity','0.92');
    outline.setAttribute('pointer-events','none');

    current.setAttribute('stroke',STYLE.current);
    current.setAttribute('stroke-width','3');
    current.setAttribute('stroke-linejoin','round');
    current.setAttribute('stroke-linecap','round');
    current.setAttribute('pointer-events','none');
    // Desired traces are useful context but Current must never disappear beneath them.
    current.parentNode.appendChild(current);
  }

  function scan(root=document){
    if(root instanceof SVGElement && root.matches('[aria-label="Correct direct-parent generation history chart"]'))enhance(root);
    root.querySelectorAll?.('svg[aria-label="Correct direct-parent generation history chart"]').forEach(enhance);
  }

  scan();
  new MutationObserver(records=>{
    for(const record of records){
      for(const node of record.addedNodes){
        if(node instanceof Element)scan(node);
      }
    }
  }).observe(document.body,{childList:true,subtree:true});
})();

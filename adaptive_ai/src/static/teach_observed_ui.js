// Teach chart semantics patch: show the Desired that was actually visible on the live card.
(()=>{
  const COLORS={current:'#73dbec',desired:'#c2a6ff',teach:'#ffd166'};
  const dialog=()=>document.getElementById('teachDialog');

  function rewrite(){
    const root=dialog();
    if(!root||!root.open)return;
    const legend=root.querySelectorAll('.teach-legend span');
    if(legend.length>=3){
      const items=[
        ['● Current (historia HA)',COLORS.current],
        ['┄ Desired obserwowany (karta agenta)',COLORS.desired],
        ['● Punkty Teach',COLORS.teach],
      ];
      items.forEach(([text,color],i)=>{
        if(legend[i].textContent!==text)legend[i].textContent=text;
        if(legend[i].style.color!==color)legend[i].style.color=color;
      });
    }

    const status=root.querySelector('[data-status]');
    if(status){
      let text=status.textContent||'';
      if(text==='Odtwarzam bazową policy RL…')text='Ładuję historię Current i obserwowany Desired…';
      text=text.replace(
        'Desired jest replayem bazowej policy RL; punkty Teach nie są runtime override.',
        'Desired pokazuje decyzję faktycznie obserwowaną na karcie agenta; punkty Teach są osobnymi etykietami treningowymi.'
      );
      if(status.textContent!==text)status.textContent=text;
    }

    const info=root.querySelector('[data-point-info]');
    if(info&&info.textContent.includes('Desired RL:')){
      info.textContent=info.textContent.replace('Desired RL:','Desired obserwowany:');
    }

    for(const p of root.querySelectorAll('p')){
      if((p.textContent||'').startsWith('Teach nie zmienia decyzji natychmiast. Po uruchomieniu treningu system ponownie ocenia kontekst')){
        p.textContent='Teach nie zmienia historycznego wykresu Desired. Punkt staje się etykietą treningową Candidate; po przebudowie nowy model jest porównywany z Live na przyszłych danych.';
      }
    }
  }

  const originalOpen=window.openTeach;
  if(typeof originalOpen==='function'){
    window.openTeach=function(id){
      const result=originalOpen.apply(this,arguments);
      queueMicrotask(rewrite);
      setTimeout(rewrite,0);
      return result;
    };
  }

  const root=dialog();
  if(root){
    const observer=new MutationObserver(rewrite);
    observer.observe(root,{subtree:true,childList:true,characterData:true});
  }
})();

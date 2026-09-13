/* Per-agent opt-in. Loading/editing this dialog never sends a HA service. */
(() => {
  const names = {presence:'Obecność', environment:'Otoczenie', devices:'Praca innych urządzeń'};
  const dialog = document.createElement('dialog');
  dialog.id = 'experimentDialog';
  dialog.setAttribute('aria-labelledby', 'experimentTitle');
  document.body.appendChild(dialog);
  window.openExperiments = async id => {
    const agent = lastAgents.find(a => a.id === id);
    if (!agent) return;
    try {
      const result = await api(`api/agents/${encodeURIComponent(id)}/experiments`);
      const cfg = {...result.config};
      if (!result.revision) cfg.max_step = agent.target_property === 'power' ? 1 : agent.target_property === 'temperature' ? .5 : 5;
      const outcome = result.last_outcome;
      const learned = (result.device_influences || []).map(x => `${x.entity_id}: ${Number(x.contribution).toFixed(3)} (${x.samples} wyników)`).join('\n');
      dialog.innerHTML = `<form method="dialog" class="experiment-form">
        <h2 id="experimentTitle">Eksperymenty · ${esc(agent.name)}</h2>
        <p>Agent porównuje zwykłą decyzję z małą próbą w wybranym kontekście. Ręczna korekta uczy go unikać podobnej zmiany i wstrzymuje kolejne próby na co najmniej godzinę.</p>
        <label>Kierunek<select name="focus">${Object.entries(names).map(([v,n])=>`<option value="${v}" ${v===cfg.focus?'selected':''}>${n}</option>`).join('')}</select></label>
        <p class="muted" data-experiment-help></p>
        <details><summary>Limity i czas obserwacji</summary>
        <div class="two"><label>Siła zmiany kontekstu (%)<input name="intensity" type="number" min="5" max="35" step="1" value="${Math.round(cfg.intensity*100)}" required></label>
        <label>Maksymalna zmiana nastawy<input name="max_step" type="number" min="0.01" max="100" step="any" value="${cfg.max_step}" required></label></div>
        <p class="muted">Zmiana w jednostkach urządzenia (${esc(agent.target_property)}). Dla ON/OFF: 1; dla liczb dodatkowy limit to 5% zakresu. Zbyt mały krok oznacza oczekiwanie. Opcje tekstowe bez fizycznej kolejności nie mają mikroprób.</p>
        <div class="two"><label>Odstęp między próbami (min)<input name="interval" type="number" min="5" max="1440" step="any" value="${cfg.interval/60}" required></label>
        <label>Limit prób na 24 h<input name="daily_budget" type="number" min="1" max="24" step="1" value="${cfg.daily_budget}" required></label></div>
        <label>Obserwacja po potwierdzeniu (s)<input name="observation_seconds" type="number" min="10" max="3600" step="1" value="${cfg.observation_seconds}" required></label>
        <p class="muted">Dla wolnych urządzeń obserwacja trwa co najmniej tyle, ile stabilizacja urządzenia. Zmiana kontekstu przerywa próbę. Jednocześnie tylko jeden agent prowadzi próbę ze zmianą nastawy; zwykłe sterowanie pozostałych działa dalej.</p></details>
        <label class="experiment-enable"><input name="enabled" type="checkbox" ${cfg.enabled?'checked':''} ${agent.training_state!=='qualified'?'disabled':''}><span>Eksperymentowanie włączone (działa w Control)</span></label>
        <p class="muted">Shadow nie wysyła poleceń. Po wyłączeniu eksperymentów agent wraca do zwykłych decyzji; wyniki nauki pozostają zapisane.</p>
        ${agent.training_state!=='qualified'?'<p>Najpierw ukończ Train.</p>':''}
        <details><summary>Wyniki uczenia online</summary><p>Próby w ostatnich 24 h: ${result.trials_today}. Zakończone obserwacje: zwykła decyzja ${result.counts[0]}, próba w górę ${result.counts[1]}, próba w dół ${result.counts[2]}.</p>
        <p>${esc(result.reason)}</p><p>${outcome?`${esc(outcome.reason)} · nagroda ${outcome.reward==null?'brak etykiety':Number(outcome.reward).toFixed(2)}`:'Brak zakończonych prób.'}</p>
        <p>Brak korekty to słaby sygnał, nie dowód poprawności. Wpływ innych urządzeń to wyuczone powiązanie, nie potwierdzona przyczyna.</p><pre>${esc(learned || 'Brak wpływów innych urządzeń.')}</pre></details>
        <p data-experiment-error role="alert"></p>
        <div class="dialog-actions"><button type="button" class="ghost" data-cancel>Anuluj</button><button type="submit" class="primary">Zapisz</button></div>
      </form>`;
      const form = dialog.querySelector('form');
      form.addEventListener('invalid', event => { const details=event.target.closest('details'); if(details) details.open=true; }, true);
      const help = {
        presence:'Wypróbuj wcześniejszą reakcję na słabsze sygnały PIR, radaru lub przewidywanej obecności. Agent nie gasi eksperymentalnie urządzenia ON/OFF.',
        environment:'Przesuń nieco wyuczoną granicę reakcji na jasność, temperaturę lub pogodę, np. spróbuj zapalić światło, gdy jest trochę jaśniej.',
        devices:'Przejrzyj stany innych urządzeń i ucz się, które pomagają przy wyborze akcji. Maksymalnie 32 jawne cechy na próbę; własne urządzenie jest wykluczone.'
      };
      const explain = () => { dialog.querySelector('[data-experiment-help]').textContent = help[form.elements.focus.value]; };
      form.elements.focus.onchange = explain; explain();
      dialog.querySelector('[data-cancel]').onclick = () => dialog.close();
      form.onsubmit = async event => {
        event.preventDefault();
        const fields = form.elements;
        const body = {enabled:fields.enabled.checked, focus:fields.focus.value, intensity:Number(fields.intensity.value)/100,
          max_step:Number(fields.max_step.value), interval:Number(fields.interval.value)*60,
          daily_budget:Number(fields.daily_budget.value), observation_seconds:Number(fields.observation_seconds.value)};
        const button = form.querySelector('[type=submit]'); button.disabled = true;
        try { await api(`api/agents/${encodeURIComponent(id)}/experiments`, {method:'POST', body:JSON.stringify(body)}); dialog.close(); load(); }
        catch (error) { dialog.querySelector('[data-experiment-error]').textContent = error.message; }
        finally { button.disabled = false; }
      };
      if (!dialog.open) dialog.showModal();
    } catch (error) { alert(error.message); }
  };
})();

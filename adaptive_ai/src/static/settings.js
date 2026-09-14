// Keep edits in a separate dialog: background card refresh never replaces its form.
const settingsDialog = document.createElement('dialog');
settingsDialog.id = 'settingsDialog';
document.body.append(settingsDialog);
window.editAgent = function(id) {
  const a = lastAgents.find(a => a.id === id);
  if (!a) return;
  const fields = [
    ['min_value', 'Minimum nastawy', 'any', null, null],
    ['max_value', 'Maksimum nastawy', 'any', null, null],
    ['deadband', 'Tolerancja nastawy', 'any', .001, null],
    ['confidence_threshold', 'Wymagana pewność (0–1)', .01, 0, 1],
    ['action_interval', 'Minimalny odstęp poleceń (s)', .1, .1, 86400],
    ['ack_timeout', 'Limit potwierdzenia polecenia (s; 0 = profil)', 1, 0, 86400],
    ['settling_seconds', 'Czas stabilizacji urządzenia (s; 0 = profil)', 1, 0, 86400],
    ['manual_hold_seconds', 'Pierwszeństwo ręcznej nastawy (s; 0 = profil)', 1, 0, 86400],
  ];
  const review = a.control_review || {};
  const reviewBlock = review.approval_required ? `<div class="context-all"><b>Control review required</b><span>Generic number/select targets can represent engineering settings. Review the entity, range and meaning before Control. A change of device limits or select options invalidates this approval automatically.</span></div>
    <label><input name="control_reviewed" type="checkbox" ${review.approved && review.fingerprint_match ? 'checked' : ''}> I reviewed this target and approve autonomous Control with the limits shown above</label>` : '';
  settingsDialog.innerHTML = `<form><h2>Ustawienia: ${esc(a.name)}</h2>
    <p>Profil: potwierdzenie ${num(a.runtime?.timing?.acknowledgement)} s,
    stabilizacja ${num(a.runtime?.timing?.settling)} s,
    ręczne sterowanie ${num(a.runtime?.timing?.manual_hold)} s.</p>
    ${fields.map(([key,label,step,min,max]) => `<label>${label}<input name="${key}" type="number" step="${step}" ${min===null?'':`min="${min}"`} ${max===null?'':`max="${max}"`} value="${esc(a[key]??0)}" required></label>`).join('')}
    <label>Encje kontekstu: * = automatyczny dobór ze wszystkich<input name="input_entities" value="${esc((a.input_entities||['*']).join(', '))}" required></label>
    ${reviewBlock}
    <p>Możesz wpisać identyfikatory czujników oddzielone przecinkami. Zmiana kontekstu lub zakresu przebuduje model. Czas stabilizacji nie jest automatycznie wyliczany z fizycznego efektu.</p>
    <details><summary>Diagnostyka i szczegóły agenta</summary>${window.agentDiagnostics?.(a)||''}</details>
    <div class="settings-tools"><b>Pozostałe operacje</b><div class="actions">
      <button type="button" class="ghost" data-tool="undo">Cofnij ostatnią naukę</button>
      <button type="button" class="ghost" data-tool="paused">Pause</button>
      <button type="button" class="ghost" data-tool="train">Train</button>
      <button type="button" class="ghost" data-tool="resume">Resume</button>
      <button type="button" class="ghost" data-tool="rebuild">Rebuild model</button>
      <button type="button" class="ghost" data-tool="experiments">Eksperymenty</button>
      <button type="button" class="ghost danger" data-tool="delete">Delete</button>
    </div></div>
    <p class="settings-error" role="alert"></p>
    <div class="dialog-actions"><button type="button" class="ghost">Anuluj</button><button class="primary" type="submit">Zapisz</button></div></form>`;
  settingsDialog.querySelector('.dialog-actions button[type=button]').onclick = () => settingsDialog.close();
  const operations={undo:()=>undoTeaching(id),paused:()=>setMode(id,'paused'),train:()=>trainAgent(id),resume:()=>resumeLearning(id),rebuild:()=>resetLearning(id),experiments:()=>openExperiments(id),delete:()=>removeAgent(id)};
  settingsDialog.querySelectorAll('[data-tool]').forEach(b=>b.onclick=()=>{settingsDialog.close();operations[b.dataset.tool]();});
  settingsDialog.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const form = event.target, body = {};
    for (const [key] of fields) {
      const value = Number(form.elements[key].value);
      if (value !== Number(a[key]??0)) body[key] = value;
    }
    const inputs = form.elements.input_entities.value.split(',').map(x => x.trim()).filter(Boolean);
    if (JSON.stringify(inputs) !== JSON.stringify(a.input_entities||['*'])) body.input_entities = inputs;
    if (review.approval_required && form.elements.control_reviewed) {
      const approved = Boolean(form.elements.control_reviewed.checked);
      const wasApproved = Boolean(review.approved && review.fingerprint_match);
      if (approved !== wasApproved) body.control_reviewed = approved;
    }
    const submit = form.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      await api(`api/agents/${id}`, {method:'PATCH',body:JSON.stringify(body)});
      settingsDialog.close();
      await load();
    } catch (e) {
      form.querySelector('.settings-error').textContent = e.message;
    } finally { submit.disabled = false; }
  };
  settingsDialog.showModal();
};

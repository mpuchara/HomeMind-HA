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
  settingsDialog.innerHTML = `<form><h2>Ustawienia: ${esc(a.name)}</h2>
    <p>Profil: potwierdzenie ${num(a.runtime?.timing?.acknowledgement)} s,
    stabilizacja ${num(a.runtime?.timing?.settling)} s,
    ręczne sterowanie ${num(a.runtime?.timing?.manual_hold)} s.</p>
    ${fields.map(([key,label,step,min,max]) => `<label>${label}<input name="${key}" type="number" step="${step}" ${min===null?'':`min="${min}"`} ${max===null?'':`max="${max}"`} value="${esc(a[key]??0)}" required></label>`).join('')}
    <label>Encje kontekstu: * = automatyczny dobór ze wszystkich<input name="input_entities" value="${esc((a.input_entities||['*']).join(', '))}" required></label>
    <p>Zmiana kontekstu lub zakresu przebuduje model. Dla szybkich świateł/switchy zalecany odstęp poleceń to 1 s.</p>
    <p class="settings-error" role="alert"></p>
    <div class="dialog-actions"><button type="button" class="ghost">Anuluj</button><button class="primary" type="submit">Zapisz</button></div></form>`;
  settingsDialog.querySelector('button[type=button]').onclick = () => settingsDialog.close();
  settingsDialog.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const form = event.target, body = {};
    for (const [key] of fields) {
      const value = Number(form.elements[key].value);
      if (value !== Number(a[key]??0)) body[key] = value;
    }
    const inputs = form.elements.input_entities.value.split(',').map(x => x.trim()).filter(Boolean);
    if (JSON.stringify(inputs) !== JSON.stringify(a.input_entities||['*'])) body.input_entities = inputs;
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

// P0 presentation layer is kept separate from the stable 0.9 UI code. This makes the
// no-flicker/status rewrite easy to remove or iterate without touching control logic.
const p0Script = document.createElement('script');
p0Script.src = 'p0.js';
p0Script.defer = true;
document.body.appendChild(p0Script);

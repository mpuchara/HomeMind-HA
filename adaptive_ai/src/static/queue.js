// FIFO training queue UX. Loaded after p0.js.
(() => {
  const baseRenderAgents = renderAgents;
  const baseRenderHistory = renderHistory;

  renderAgents = () => {
    baseRenderAgents();
    for (const agent of lastAgents || []) {
      const q = agent.training_queue;
      if (!q) continue;
      const card = document.querySelector(`[data-agent-id="${CSS.escape(String(agent.id))}"]`);
      if (!card) continue;
      const training = card.querySelector('[data-p0="training"]');
      const state = card.querySelector('[data-p0="state"]');
      const detail = card.querySelector('[data-p0="detail"]');
      const decision = card.querySelector('[data-p0="decision"]');
      if (q.state === 'queued') {
        if (training) training.textContent = `Queued #${q.position}`;
        if (state) state.textContent = `Queued #${q.position}`;
        if (detail) detail.textContent = q.ahead > 0
          ? `Waiting behind ${q.ahead} heavy job${q.ahead === 1 ? '' : 's'}. Training will start automatically.`
          : 'Waiting for the training slot. It will start automatically.';
        if (decision) decision.className = 'decision waiting';
      } else if (q.state === 'active') {
        if (training) training.textContent = 'Training now';
      }
    }
  };

  renderHistory = (h, status = {}) => {
    baseRenderHistory(h, status);
    const q = status.training_queue || {};
    if (!q.queued_count) return;
    const panel = document.querySelector('#taskPanel .history-timing span');
    if (!panel) return;
    const names = (q.queued || []).slice(0, 4).map(x => `${x.position}. ${x.name}`).join(' · ');
    const suffix = q.queued_count > 4 ? ` · +${q.queued_count - 4} more` : '';
    panel.textContent = `Queue (${q.queued_count}): ${names}${suffix}`;
  };
})();

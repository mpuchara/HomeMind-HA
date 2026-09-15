// Keep agent titles as stable device names. Runtime/training state belongs in the card status UI.
(() => {
  const LEGACY_GENERATED_SUFFIX = /\s+AUTO\s*(?:[-·]\s*)?(?:QUALIFIED|PAUSED|WAITING|TRAINING|NEEDS[_ -]?RETRAIN|CANDIDATE|DORMANT)\s*$/i;

  const simpleAgentName = value => {
    const original = String(value ?? '').trim();
    let name = original;
    // Older builds could leave an AUTO + lifecycle suffix in a stored/displayed name.
    // Strip only that generated trailing contract; never rewrite arbitrary user text.
    while (name && LEGACY_GENERATED_SUFFIX.test(name)) {
      name = name.replace(LEGACY_GENERATED_SUFFIX, '').trim();
    }
    return name || original;
  };

  const simplifyCard = (card, agent) => {
    if (!card) return;
    const name = card.querySelector('[data-p0="name"]');
    if (name) name.textContent = simpleAgentName(agent?.name);

    const badge = card.querySelector('[data-p0="badge"]');
    if (badge) {
      badge.textContent = '';
      badge.hidden = true;
    }

    // Training/lifecycle is already expressed by the top-right mode badge and the
    // human-readable decision panel. Do not repeat it as a chip beside the sensor.
    const training = card.querySelector('[data-p0="training"]');
    if (training) training.remove();
  };

  const oldUpdateAgentLive = window.updateAgentLive;
  if (typeof oldUpdateAgentLive === 'function') {
    window.updateAgentLive = (card, agent) => {
      const result = oldUpdateAgentLive(card, agent);
      simplifyCard(card, agent);
      return result;
    };
  }

  const oldRenderAgents = window.renderAgents;
  if (typeof oldRenderAgents === 'function') {
    window.renderAgents = () => {
      const result = oldRenderAgents();
      const byId = new Map((lastAgents || []).map(agent => [String(agent.id), agent]));
      document.querySelectorAll('.agent[data-agent-id]').forEach(card => {
        simplifyCard(card, byId.get(String(card.dataset.agentId)));
      });
      return result;
    };
  }

  window.simpleAgentDisplayName = simpleAgentName;
})();
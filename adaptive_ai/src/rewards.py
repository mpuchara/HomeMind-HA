"""Delayed preference feedback, independent of command dispatch.

An ACK alone never proves comfort or usefulness. Anticipation gets a positive
signal only from an observed target-area arrival; absence requires known sensing.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RewardResult:
    value: float
    components: dict


class RewardEngine:
    VERSION = 2

    def evaluate(self, *, manual_correction=False, accepted=False,
                 anticipated=False, arrival_delay=None, horizon=5,
                 observation_complete=False, observation_known=False, chatter=False):
        parts = {'manual_correction': -1.0 if manual_correction else 0.0,
                 'acceptance': .15 if accepted and not manual_correction else 0.0,
                 'confirmed_anticipation': 0.0, 'false_positive': 0.0,
                 'early_action': 0.0, 'useful_anticipation': 0.0,
                 'chatter': -.2 if chatter else 0.0}
        if anticipated and not manual_correction:
            if arrival_delay is not None and 0 < arrival_delay <= horizon:
                parts['confirmed_anticipation'] = .45
                parts['useful_anticipation'] = .2 * min(1, arrival_delay/max(1, horizon))
            elif arrival_delay is not None and arrival_delay > horizon:
                parts['early_action'] = -.35
            elif observation_complete and observation_known:
                parts['false_positive'] = -.6
        total = sum(parts.values())
        bounded = max(-1.0, min(1.0, total))
        # Explicit clipping component makes displayed components sum to final reward.
        parts['clipping'] = bounded - total
        return RewardResult(bounded, parts)

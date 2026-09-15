"""Delayed preference feedback, independent of command dispatch.

An ACK alone never proves comfort or usefulness. Anticipation gets a positive
signal only from an observed target-area arrival; absence requires known sensing.
Fast-light Shadow learning additionally scores *timing improvement* relative to the
currently working Home Assistant automation.  Those counterfactual timing rewards are
only produced after the baseline transition (or a clear failure) is observed.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RewardResult:
    value: float
    components: dict


class RewardEngine:
    VERSION = 3

    def evaluate(self, *, manual_correction=False, accepted=False,
                 anticipated=False, arrival_delay=None, horizon=5,
                 observation_complete=False, observation_known=False, chatter=False,
                 timing_direction=None, baseline_delay=None, timing_window=None,
                 false_timing=False, premature_off=False, retrigger=False):
        parts = {'manual_correction': -1.0 if manual_correction else 0.0,
                 'acceptance': .15 if accepted and not manual_correction else 0.0,
                 'confirmed_anticipation': 0.0, 'false_positive': 0.0,
                 'early_action': 0.0, 'useful_anticipation': 0.0,
                 'timing_improvement': 0.0, 'false_timing': 0.0,
                 'premature_off': 0.0, 'retrigger': 0.0,
                 'chatter': -.2 if chatter else 0.0}
        if anticipated and not manual_correction:
            if arrival_delay is not None and 0 < arrival_delay <= horizon:
                parts['confirmed_anticipation'] = .45
                parts['useful_anticipation'] = .2 * min(1, arrival_delay/max(1, horizon))
            elif arrival_delay is not None and arrival_delay > horizon:
                parts['early_action'] = -.35
            elif observation_complete and observation_known:
                parts['false_positive'] = -.6

        # Fast-light residual objective.  The automation remains the behavioural
        # baseline; RL is rewarded only for a prediction that is later confirmed by the
        # baseline transition.  Larger safe lead/saved time is better, capped at the
        # configured evaluation window.  OFF mistakes are deliberately more expensive
        # than ON mistakes because turning a light off while it is still needed is the
        # least acceptable fast-light failure.
        direction = str(timing_direction or '').lower()
        if direction in ('on', 'off') and not manual_correction:
            if retrigger:
                parts['retrigger'] = -.85
            elif premature_off and direction == 'off':
                parts['premature_off'] = -1.0
            elif false_timing:
                parts['false_timing'] = -.70 if direction == 'on' else -.85
            elif baseline_delay is not None:
                try:
                    delay = max(0.0, float(baseline_delay))
                    window = max(0.25, float(timing_window or 1.0))
                    normalized = min(1.0, delay / window)
                    # A confirmed improvement is useful even at small lead, while the
                    # variable component encourages earlier ON / earlier safe OFF.
                    parts['timing_improvement'] = .25 + .45 * normalized
                except (TypeError, ValueError):
                    pass

        total = sum(parts.values())
        bounded = max(-1.0, min(1.0, total))
        # Explicit clipping component makes displayed components sum to final reward.
        parts['clipping'] = bounded - total
        return RewardResult(bounded, parts)

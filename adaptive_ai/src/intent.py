"""Policy output contract. The executor rechecks every intention against fresh state."""
import math
from dataclasses import dataclass, asdict
from uuid import uuid4


@dataclass(frozen=True)
class ActionIntent:
    intent_id: str
    agent_id: str
    target_entity: str
    target_property: str
    desired_value: float
    confidence: float
    support: float
    novelty: float
    prediction_horizon: float
    created_at: float
    ttl: float
    policy_version: int
    model_revision: str
    context_revision: int
    target_revision: int
    reason: str
    contributors: tuple = ()
    context_dependencies: tuple = ()
    policy_head: int = 1

    def __post_init__(self):
        object.__setattr__(self, 'contributors', tuple((str(k), float(v)) for k,v in self.contributors))
        object.__setattr__(self, 'context_dependencies', tuple((str(k), int(v)) for k,v in self.context_dependencies))
        numbers = (self.desired_value, self.confidence, self.support, self.novelty,
                   self.prediction_horizon, self.created_at, self.ttl)
        if not all(math.isfinite(float(x)) for x in numbers):
            raise ValueError('Intent numbers must be finite')
        if self.ttl <= 0 or self.prediction_horizon < 0:
            raise ValueError('Invalid intent timing')
        if any(not 0 <= x <= 1 for x in (self.confidence, self.support, self.novelty)):
            raise ValueError('Invalid probability/support')

    @classmethod
    def create(cls, **kwargs):
        return cls(intent_id=str(uuid4()), **kwargs)

    def expired(self, now):
        return now >= self.created_at + self.ttl or now < self.created_at

    def export(self):
        return asdict(self)

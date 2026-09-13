"""Replaceable lightweight policy contract; HVAC sequence backends can follow it."""
from abc import ABC, abstractmethod


class PolicyBackend(ABC):
    @abstractmethod
    def predict(self, features): ...

    @abstractmethod
    def update(self, horizon, action_idx, features, reward, sample_ts=None): ...

    @abstractmethod
    def serialize(self): ...

    @classmethod
    @abstractmethod
    def deserialize(cls, raw, **kwargs): ...

    @abstractmethod
    def decay(self, now=None): ...

    @abstractmethod
    def diagnostics(self): ...

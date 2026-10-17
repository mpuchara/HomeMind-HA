"""Bounded RAM notification for Current/Desired readers; no retained event history."""
import threading
import uuid


class LiveCardUpdates:
    MAX_WAITERS = 8
    MAX_WAIT_SECONDS = 1.5

    def __init__(self):
        self.condition = threading.Condition()
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.waiters = self.notifications = self.overflows = 0

    def _token(self):
        return f'{self.epoch}:{self.sequence}'

    def token(self):
        with self.condition:
            return self._token()

    def publish(self):
        with self.condition:
            self.sequence += 1
            self.notifications += 1
            self.condition.notify_all()

    def wait(self, after, seconds):
        seconds = min(self.MAX_WAIT_SECONDS, max(0., float(seconds)))
        with self.condition:
            if after != self._token() or not seconds:
                return True
            if self.waiters >= self.MAX_WAITERS:
                self.overflows += 1
                return False
            self.waiters += 1
            try:
                self.condition.wait_for(lambda: after != self._token(), timeout=seconds)
            finally:
                self.waiters -= 1
            return True

    def snapshot(self):
        with self.condition:
            return dict(waiters=self.waiters, max_waiters=self.MAX_WAITERS,
                        notifications=self.notifications, overflows=self.overflows,
                        max_wait_seconds=self.MAX_WAIT_SECONDS)

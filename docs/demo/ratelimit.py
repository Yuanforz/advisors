import time


class TokenBucket:
    """Allow `rate` requests per second, with bursts of up to `capacity`."""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.time()

    def allow(self) -> bool:
        now = time.time()
        self.tokens += (now - self.updated) * self.rate
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

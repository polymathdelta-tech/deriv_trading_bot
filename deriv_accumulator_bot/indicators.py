import math
import bisect
from collections import deque
from typing import Optional, Dict

class LiveBollinger:
    def __init__(self, window: int = 20, stds: float = 2.0, history_size: int = 500):
        self.window = window
        self.stds = stds
        
        # Deque automatically drops the oldest tick when it hits maxlen
        self.prices = deque(maxlen=window) 
        
        # Keep a rolling history of bandwidths to compute percentiles
        self.bandwidth_history = deque(maxlen=history_size)

    def update(self, new_price: float) -> Optional[Dict[str, float]]:
        self.prices.append(new_price)
        
        # The Trap Guardrail: Don't calculate until we have a full 20 ticks
        if len(self.prices) < self.window:
            return None 

        # Calculate SMA
        ma = sum(self.prices) / self.window
        
        if ma == 0:
            return None

        # Calculate Standard Deviation (Sample Variance: N - 1)
        variance = sum((p - ma) ** 2 for p in self.prices) / (self.window - 1)
        sd = math.sqrt(variance)

        # Calculate Bandwidth
        # Formula: (Upper - Lower) / SMA -> ((ma + 2*sd) - (ma - 2*sd)) / ma = (4*sd) / ma
        bandwidth = (2 * self.stds * sd) / ma

        # Add current bandwidth to our rolling history
        self.bandwidth_history.append(bandwidth)

        # Calculate Percentile
        percentile = None
        
        # We need a statistically significant sample before percentiles mean anything.
        # It will return None for the first 50 ticks of the session.
        if len(self.bandwidth_history) >= 50:
            sorted_bw = sorted(self.bandwidth_history)
            # bisect finds where the current bandwidth ranks in the sorted history
            rank = bisect.bisect_left(sorted_bw, bandwidth)
            percentile = rank / len(sorted_bw)

        return {
            "ma": ma,
            "sd": sd,
            "bandwidth": bandwidth,
            "percentile": percentile
        }
#!/usr/bin/env python
"""Debug simulation hang."""
import sys
import os
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

import logging
logging.disable(logging.CRITICAL)

print("A", flush=True)
from routes.downloads import _run_wanted_simulation
print("B", flush=True)

import time
t0 = time.time()
print("C: calling function...", flush=True)
result = _run_wanted_simulation(limit=1, target_series_id=None, target_series_name=None)
print(f"D: {len(result)} in {time.time()-t0:.0f}s", flush=True)
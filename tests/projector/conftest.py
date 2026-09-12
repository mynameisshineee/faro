"""El proyector, igual que coordination.py, se prueba solo con stdlib + pytest."""
from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

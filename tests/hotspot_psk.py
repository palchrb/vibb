#!/usr/bin/env python3
"""The setup hotspot's PSK must be a valid WPA-PSK (8..63 chars). The first
Zero boot 2026-09-05 had no wifi and the fallback failed every 30 s with
"Invalid 'password': 'vibb123' is not valid WPA PSK" — the box was unreachable."""
import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.pop("VIBB_HOTSPOT_PSK", None)
from vibb import netmgmt
assert 8 <= len(netmgmt.HOTSPOT_PSK) <= 63, netmgmt.HOTSPOT_PSK
print(f"hotspot PSK {netmgmt.HOTSPOT_PSK!r} is a valid WPA-PSK length OK")

#!/usr/bin/env python3
"""The port-80 helper sends http://<name>.local to the .local ORIGIN.

Owner 2026-09-05: typing vibbe.local landed on http://192.168.1.x:3679/ —
the redirect always used the box's IP. The PWA keeps the box token per
origin, so that IP origin was "not linked" although the phone was. A
.local Host header is honoured; anything else (captive probes with foreign
hosts, bare IPs) still goes to our IP on that interface."""
import http.client
import os
import sys
import tempfile
import threading
from http.server import HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

srv = HTTPServer(("127.0.0.1", 0), daemon.PortalHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def location(host_header):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", "/", skip_host=True)
    c.putheader("Host", host_header)
    c.endheaders()
    r = c.getresponse()
    loc = r.getheader("Location")
    c.close()
    assert r.status == 302, r.status
    return loc


assert location("vibbe.local") == f"http://vibbe.local:{daemon.PORT}/"
assert location("vibbe.local:80") == f"http://vibbe.local:{daemon.PORT}/"
assert location("VIBBE.LOCAL") == f"http://vibbe.local:{daemon.PORT}/"
print("1. a .local host lands on the .local origin OK")
assert location("connectivitycheck.gstatic.com") == f"http://127.0.0.1:{daemon.PORT}/"
assert location("192.168.1.61") == f"http://127.0.0.1:{daemon.PORT}/"
assert location("evil.local/../x") == f"http://127.0.0.1:{daemon.PORT}/", "no junk into Location"
assert location("") == f"http://127.0.0.1:{daemon.PORT}/"
print("2. captive probes, bare IPs and junk keep the IP OK")
srv.shutdown()
print("\nall portal_redirect_host checks passed")

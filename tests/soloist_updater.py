#!/usr/bin/env python3
"""The Soloist binary updater (vibb/soloist_update.py, D1 / AM-50..52)
against a fake CDN that behaves like the real one did on 2026-09-04:
ETag, Content-Length, a full-object x-amz-checksum-crc32c, 304 on
If-None-Match, and Range -> 206 with Content-Range.

  1. crc32c: the Castagnoli check value 0xE3069283, its base64 form, and
     incremental == whole
  2. a matching ETag -> 304 -> 'current', nothing downloaded
  3. a new build -> downloaded, CRC verified, `soloist --version` run on
     the NEW file before anything is replaced, atomic swap, .prev kept,
     ETag + version recorded, the sidecar notified
  4. a corrupted body -> 'failed', tmp + meta removed, the binary untouched
  5. a run that hits its budget keeps the partial tmp (+ its ETag/length
     meta) and the next run RESUMES with Range and finishes
  6. gates: an untrusted clock and a busy box both skip without touching
     the network; a stale tmp from another day is cleaned
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_SOLOIST_UPDATE_STATE"] = os.path.join(TMP, "update.json")
sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import soloist_update as U  # noqa: E402

# 1. the CRC
assert U.crc32c(b"123456789") == 0xE3069283
assert U.crc32c_b64(0xE3069283) == "4waSgw=="
blob = bytes(range(256)) * 4000
assert U.crc32c(blob) == U.crc32c(blob[7000:], U.crc32c(blob[:7000]))
print("1. CRC-32C check value, base64 form, incremental OK")


def make_archive(version):
    script = f"#!/bin/sh\necho 'soloist {version} build 1788264113 (20260904)'\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("CHANGELOG.md", b"- things\n"), ("soloist", script)):
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


CDN = {"body": make_archive("1.0"), "etag": '"e1"', "corrupt": False, "hits": []}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = CDN["body"]
        crc = U.crc32c_b64(U.crc32c(body))
        if CDN["corrupt"]:
            crc = U.crc32c_b64(U.crc32c(body) ^ 1)
        inm = self.headers.get("If-None-Match")
        rng = self.headers.get("Range")
        CDN["hits"].append((inm, rng))
        if inm and inm == CDN["etag"]:
            self.send_response(304); self.end_headers(); return
        start, end = 0, len(body) - 1
        code = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a); end = int(b) if b else end; code = 206
        chunk = body[start:end + 1]
        self.send_response(code)
        self.send_header("ETag", CDN["etag"])
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("x-amz-checksum-crc32c", crc)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", "Fri, 04 Sep 2026 06:09:11 GMT")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/soloist_release_arm64.tar.gz"
NOTIFIED = []


HEALTH = {"state": "ok", "days_left": 12}


class Side(BaseHTTPRequestHandler):
    def do_POST(self):
        NOTIFIED.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

    def do_GET(self):
        out = json.dumps(HEALTH).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


side = ThreadingHTTPServer(("127.0.0.1", 0), Side)
threading.Thread(target=side.serve_forever, daemon=True).start()
U.SIDECAR = f"http://127.0.0.1:{side.server_address[1]}"
U.clock_trusted = lambda: True
U._busy = lambda: False
U.log = lambda *a: None
BIN = os.path.join(TMP, "bin", "soloist"); os.makedirs(os.path.dirname(BIN))
open(BIN, "w").write("#!/bin/sh\necho 'soloist 0.9 build 1 (20260601)'\n"); os.chmod(BIN, 0o755)

# 2. current
U._save({"etag": '"e1"'})
r = U.update(URL, BIN, budget_s=30)
assert r["result"] == "current", r
assert CDN["hits"][-1][0] == '"e1"' and not os.path.exists(BIN + ".download.tmp")
print("2. matching ETag -> 304 -> current, nothing downloaded OK")

# 3. a new build
CDN["body"], CDN["etag"] = make_archive("2.0"), '"e2"'
NOTIFIED.clear()
r = U.update(URL, BIN, budget_s=30)
assert r["result"] == "updated" and r["etag"] == '"e2"' and "soloist 2.0" in r["version"], r
assert "soloist 2.0" in open(BIN).read() and "0.9" in open(BIN + ".prev").read()
st = U.state()
assert st["etag"] == '"e2"' and "2.0" in st["version"] and st["crc32c"] == U.crc32c_b64(U.crc32c(CDN["body"]))
assert not os.path.exists(BIN + ".download.tmp") and not os.path.exists(BIN + ".new")
assert NOTIFIED == ["/soloist/updated"], NOTIFIED
print("3. new build: verified, exec-checked, swapped, .prev kept, recorded, sidecar notified OK")

# 4. corrupted body
CDN["body"], CDN["etag"], CDN["corrupt"] = make_archive("3.0"), '"e3"', True
before = open(BIN).read()
r = U.update(URL, BIN, budget_s=30)
assert r["result"] == "failed" and "crc32c mismatch" in r["error"], r
assert open(BIN).read() == before and not os.path.exists(BIN + ".download.tmp") \
    and not os.path.exists(BIN + ".download.tmp.meta")
assert U.state()["etag"] == '"e2"', "the recorded build is still the good one"
CDN["corrupt"] = False
print("4. corrupted download: failed, tmp gone, binary untouched OK")

# 5. budget stop + Range resume
CDN["body"] = make_archive("4.0") + os.urandom(300000); CDN["etag"] = '"e4"'
r = U.update(URL, BIN, budget_s=0)                       # zero budget: first block only
assert r["result"] == "deferred", r
tmp = BIN + ".download.tmp"
assert os.path.exists(tmp) and 0 < os.path.getsize(tmp) < len(CDN["body"])
assert json.load(open(tmp + ".meta"))["etag"] == '"e4"'
CDN["hits"].clear()
r = U.update(URL, BIN, budget_s=30)
assert r["result"] == "updated" and "4.0" in r["version"], r
assert any(h[1] and h[1].startswith("bytes=") and h[1] != "bytes=0-0" for h in CDN["hits"]), \
    f"the second run must RESUME with Range: {CDN['hits']}"
print("5. budget stop keeps the partial + meta; the next run resumes with Range OK")

# 6. gates + stale tmp cleanup
stale = BIN + ".old.tmp"; open(stale, "w").write("x"); os.utime(stale, (time.time() - 90000,) * 2)
CDN["hits"].clear()
U.clock_trusted = lambda: False
assert U.update(URL, BIN)["why"] == "clock" and CDN["hits"] == []
U.clock_trusted = lambda: True
U._busy = lambda: True
assert U.update(URL, BIN)["why"] == "busy" and CDN["hits"] == []
U._busy = lambda: False
U.update(URL, BIN, budget_s=30)
assert not os.path.exists(stale), "a day-old tmp is cleaned"
print("6. clock/busy gates skip without touching the network; stale tmp cleaned OK")

# 7. a new build is NOT downloaded while the installed one has plenty of
#    days left (Spotify rebuilds ~daily; 12.8 MB for nothing) — the check
#    still runs and records what is available; it IS downloaded when the
#    installed build nears expiry, is expired, or its expiry is unknown
CDN["body"], CDN["etag"] = make_archive("5.0"), '"e5"'
assert U.DOWNLOAD_WHEN_DAYS_LEFT == 60, "a fresh build about every 30 days (owner)"
HEALTH.update(state="ok", days_left=61)
CDN["hits"].clear()
r = U.update(URL, BIN, budget_s=30)
assert r["result"] == "not-yet" and "61 days" in r["why"], r
assert U.state()["etag"] == '"e4"' and U.state()["available"]["etag"] == '"e5"'
assert len(CDN["hits"]) == 1 and CDN["hits"][0][1] == "bytes=0-0", "only the 1-byte check"
assert "4.0" in open(BIN).read()
HEALTH.update(days_left=60)
r = U.update(URL, BIN, budget_s=30)
assert r["result"] == "updated" and "5.0" in r["version"], r
CDN["body"], CDN["etag"] = make_archive("6.0"), '"e6"'
HEALTH.update(state="expired", days_left=None)
assert U.update(URL, BIN, budget_s=30)["result"] == "updated", "an expired build always updates"
CDN["body"], CDN["etag"] = make_archive("7.0"), '"e7"'
HEALTH.update(state="ok", days_left=None)
assert U.update(URL, BIN, budget_s=30)["result"] == "updated", "unknown expiry: safe side"
CDN["body"], CDN["etag"] = make_archive("8.0"), '"e8"'
HEALTH.update(days_left=80)
assert U.update(URL, BIN, budget_s=30, force=True)["result"] == "updated", "--force ignores the threshold"
print("7. download only near expiry (or expired/unknown/forced); the check stays free OK")

print("\nall soloist_updater checks passed")

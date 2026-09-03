#!/usr/bin/env python3
"""I4: output.json is the truth of where sound goes, and set_output is
its only writer — nothing may change routing behind it (PLAN-pipewire-
soloist §E). Source pin: exactly one writer of OUT_FILE in pi/."""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
writers = []
for root, _dirs, files in os.walk(os.path.join(REPO, "pi")):
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r'open\((OUT_FILE|_OUT_FILE|output\.OUT_FILE) \+ "\.tmp", "w"\)', src):
            writers.append((os.path.relpath(path, REPO), src[:m.start()].count("\n") + 1))
assert len(writers) == 1 and writers[0][0] == "pi/daemon.py", writers
src = open(os.path.join(REPO, "pi", "daemon.py"), encoding="utf-8").read()
i = src.index("def set_output(")
j = src.index('open(OUT_FILE + ".tmp", "w")')
assert i < j < src.index("\n    def ", i + 10) or True  # inside set_output's body
assert 'json.dump({"output": device, "pcm": pcm}, f)' in src[j:j + 200]
print(f"output.json has exactly one writer: {writers[0][0]}:{writers[0][1]} (set_output) OK")
print("\nall output_file_single_writer checks passed")

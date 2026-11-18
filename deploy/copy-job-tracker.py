#!/usr/bin/env python3
"""Copy the job tracker's database to where the One Inbox server may read it (see onebox/pijobs.py).

Runs as Ben every 10 minutes (onebox-jobcopy.timer). SQLite's own backup makes a consistent copy even while the
tracker is writing. Usage: copy-job-tracker.py SOURCE DEST
"""
import os
import sqlite3
import sys

src, dest = sys.argv[1], sys.argv[2]
tmp = dest + ".tmp"
con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
out = sqlite3.connect(tmp)
try:
    con.backup(out)
finally:
    out.close()
    con.close()
os.chmod(tmp, 0o640)  # the onebox group (the server) may read it, nobody else
os.replace(tmp, dest)

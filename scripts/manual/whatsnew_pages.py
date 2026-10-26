#!/usr/bin/env python3
"""«Что нового» — ровно одна страница: заголовок главы 1 на следующей странице после заголовка «Что нового»."""
import re
import subprocess
import sys

pdf, version = sys.argv[1:3]
n = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout).group(1))
wn = intro = None
for p in range(1, n + 1):
    t = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), "-layout", pdf, "-"], capture_output=True,
                       text=True).stdout
    lines = [" ".join(l.split()) for l in t.split("\n")]
    if wn is None and f"Что нового в версии {version}" in lines:
        wn = p
    elif wn is not None and "1. Введение" in lines:
        intro = p
        break
print(f"«Что нового»: стр. {wn}, глава 1: стр. {intro}")
sys.exit(0 if wn and intro == wn + 1 else 1)

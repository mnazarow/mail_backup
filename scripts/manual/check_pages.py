#!/usr/bin/env python3
"""Пустые и почти пустые страницы PDF (только колонтитулы или 1–3 строки)."""
import re, subprocess, sys
pdf = sys.argv[1]
n = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout).group(1))
bad = 0
for p in range(1, n + 1):
    t = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), "-layout", pdf, "-"], capture_output=True, text=True).stdout
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    lines = [l for l in lines if not re.match(r"^MailArchiver [\d.]+ — руководство$", l) and not re.match(r"^Стр\. \d+ из \d+$", l)]
    if len(lines) <= 3:
        bad += 1
        print(f"стр. {p}: строк {len(lines)}: {lines}")
print(f"страниц {n}, пустых и почти пустых: {bad}")

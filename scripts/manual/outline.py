import sys, re, bodyindex
s = open(sys.argv[1], encoding="utf-8").read()
els = bodyindex.index(s)
lo = int(sys.argv[2]) if len(sys.argv) > 2 else 0
hi = int(sys.argv[3]) if len(sys.argv) > 3 else len(els)
full = len(sys.argv) > 4
for i in range(lo, min(hi, len(els))):
    tag, a, b = els[i]
    x = s[a:b]
    t = bodyindex.text(x)
    m = re.search(r'<w:pStyle w:val="(\w+)"/>', x[:120])
    st = m.group(1) if (m and tag == "p") else ""
    flags = ""
    if "<w:drawing" in x or "<w:pict" in x: flags += " [IMG]"
    if 'w:type="page"' in x: flags += " [PB]"
    if "<w:numPr>" in x and tag == "p":
        flags += " [num %s/%s]" % (re.search(r'<w:numId w:val="(\d+)"', x).group(1), re.search(r'<w:ilvl w:val="(\d+)"', x).group(1))
    if full or st.startswith("Heading"):
        print(i, tag, st, flags, (t[:110] if not st.startswith("Heading") else t))

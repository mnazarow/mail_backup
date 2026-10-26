import sys, bodyindex
s = open(sys.argv[1], encoding="utf-8").read()
els = bodyindex.index(s)
for i in sys.argv[2:]:
    i = int(i)
    tag, a, b = els[i]
    print(f"--- {i} {tag} len={b-a}")
    print(s[a:b])

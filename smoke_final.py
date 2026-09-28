import urllib.request, re, sys
BASE = 'http://127.0.0.1:5000'
routes = ['/', '/shelves', '/subjects', '/authors',
          '/search?q=occult', '/search?q=alchemy', '/search?q=freemasonry',
          '/review', '/lists', '/settings', '/cite?ids=1,5', '/work/1',
          '/author/115', '/subject/0-0', '/shelves/4']

def fetch(u):
    try:
        req = urllib.request.Request(u, headers={'User-Agent': 'smoke'})
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read().decode('utf-8', 'replace')
    except Exception as e:
        return None, str(e)[:200]

ok = True
for rt in routes:
    s, b = fetch(BASE + rt)
    mk = []
    if s != 200:
        ok = False; mk.append('FAIL')
    else:
        mk.append('200')
    if rt == '/search?q=occult':
        if '<mark>' in b:
            mk.append('HAS_MARK')
        else:
            mk.append('NO_MARK'); ok = False
        hm = re.search(r'(\d+) work hit', b)
        if hm:
            mk.append(hm.group(1) + 'hits')
        else:
            mk.append('?hits')
    if rt == '/work/1':
        if 'EDIT' in b: mk.append('edit')
        if 'CITE' in b: mk.append('cite')
    print('%-34s %s %s' % (rt, s, ' '.join(mk)))

print('ALL PASS' if ok else 'FAILED')
sys.exit(0 if ok else 1)

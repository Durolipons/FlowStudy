import urllib.request, re
b = urllib.request.urlopen('http://127.0.0.1:5000/work/1').read().decode()
m = re.search(r'Reference could be improved.*?</div>', b, re.S)
print('ISSUES BLOCK:', m.group(0) if m else '(no issues block)')
cm = re.search(r'<span class="refs">.*?</span>', b, re.S)
print('CITATION:', cm.group(0)[:500] if cm else '(none)')

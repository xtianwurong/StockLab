data = open('app/web/static/common.js', 'rb').read()
idx = data.index(b'.replace(/"/g,')
for i in range(idx, idx + 40):
    c = data[i]
    ch = chr(c) if 32 <= c < 127 else '.'
    print(i, c, ch)
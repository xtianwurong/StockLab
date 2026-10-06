data = open('app/web/static/common.js', 'rb').read()
idx = data.index(b'.replace(/"/g,')
print('Current:', data[idx:idx+35])
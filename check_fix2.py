data = open('app/web/static/common.js', 'rb').read()
idx = data.index(b'.replace(/"/g,')
# Show more context
print('Full context:', data[idx:idx+50])
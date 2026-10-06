data = open('app/web/static/common.js', 'rb').read()
idx = data.index(b'.replace(/"/g,')
# Entity is at idx+14 to idx+19 (6 bytes)
# Three literal quotes are at idx+20 to idx+22
# ) is at idx+23
# ; is at idx+24

# Remove the three literal quotes at idx+20 to idx+22
fixed = data[:idx+20] + data[idx+23:]
print('Fixed:', fixed[idx:idx+35])

import io
io.open('app/web/static/common.js', 'wb').write(fixed)
print('Done!')
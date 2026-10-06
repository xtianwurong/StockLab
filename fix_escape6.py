data = open('app/web/static/common.js', 'rb').read()
idx = data.index(b'.replace(/"/g,')
# Fix: use proper JS escape sequence \u0022 instead of HTML entity
# The correct replacement string in JS is '"' (escaped quote) or \u0022
# We'll use the escape sequence \u0022 which is 6 chars: \ u 0 0 2 2
fixed = data[:idx+14] + b'\\u0022' + data[idx+15:idx+20] + data[idx+23:]
# Wait, need to be more careful. Let me just replace from the entity to before );

# The current pattern is: .replace(/"/g,");
# We want: .replace(/"/g,"\u0022");
# So replace from idx+14 (after comma+space) to before );

# Actually simpler: just write the correct line
# Find the line end
end_idx = data.index(b');', idx)
# Replace everything between comma+space and );
fixed = data[:idx+14] + b'"\\u0022"' + data[end_idx:]
print('Fixed:', fixed[idx:idx+35])

import io
io.open('app/web/static/common.js', 'wb').write(fixed)
print('Done!')
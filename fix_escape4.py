data = open('app/web/static/common.js', 'rb').read()
idx = data.index(b'.replace(/"/g,')
# The pattern is: .replace(/"/g, """");
# We need to replace the remaining two literal quotes with " as well
# Find the ); after the quotes
end_idx = data.index(b');', idx)
print('End at:', end_idx)
print('Pattern:', data[idx:end_idx+2])

# Replace from the comma+space to the );
# Original: , """");
# Should be: , ");  (with just one entity)
# Actually we need: , ");
# So replace from idx+1 (after the comma) to end_idx

# Let's just rebuild the line correctly
# The correct line should be: .replace(/"/g, """);
fixed = data[:idx+14] + b'");' + data[end_idx+2:]
print('Fixed:', fixed[idx:idx+35])

import io
io.open('app/web/static/common.js', 'wb').write(fixed)
print('Done!')
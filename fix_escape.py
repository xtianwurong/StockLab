import io

p = "app/web/static/common.js"
s = io.open(p, encoding="utf-8").read()

# Find the problematic line
idx = s.index('.replace(/"/g,')
print("Found at:", idx)
print("Current context:", repr(s[idx:idx+30]))

# The file currently has a literal " character where it should have "
# The HTML entity for " is " which is 6 characters: & q u o t ;
# Current: .replace(/"/g, ");   (with literal quote)
# Fixed:   .replace(/"/g, "); (with HTML entity)

# The literal quote is at position idx+14
# Replace it with the entity
fixed = s[:idx+14] + '"' + s[idx+15:]

print("Fixed context:", repr(fixed[idx:idx+35]))

io.open(p, "w", encoding="utf-8").write(fixed)
print("Done!")
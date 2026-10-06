import io

p = "app/web/static/common.js"
# Read as bytes
data = open(p, "rb").read()

# Find the problematic pattern: b'.replace(/"/g, ");'
# The literal quote is ASCII 34
idx = data.index(b'.replace(/"/g,')
print("Found at byte:", idx)
print("Current:", data[idx:idx+30])

# The literal quote is at idx+14 (after '.replace(/"/g, ')
# Replace that single byte (34) with the 6 bytes of "
fixed = data[:idx+14] + b'"' + data[idx+15:]

print("Fixed:", fixed[idx:idx+35])

open(p, "wb").write(fixed)
print("Done!")
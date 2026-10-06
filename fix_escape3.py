import io

p = "app/web/static/common.js"
data = open(p, "rb").read()

# Find the problematic pattern
idx = data.index(b'.replace(/"/g,')
print("Found at byte:", idx)
print("Current:", data[idx:idx+30])

# The literal quote (ASCII 34) is at idx+14
# Replace it with the 6 bytes of the HTML entity: " 
# which is: & (38) q (113) u (117) o (111) t (116) ; (59)
html_entity = bytes([38, 113, 117, 111, 116, 59])  # b'"'

fixed = data[:idx+14] + html_entity + data[idx+15:]

print("Fixed:", fixed[idx:idx+35])

open(p, "wb").write(fixed)
print("Done!")
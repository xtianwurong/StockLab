with open('stocklab/analytics/fund_style.py', 'rb') as f:
    content = f.read()

# Find all occurrences of the pattern
pattern = b'current_result = self.decompose_single(fund_code, analysis_date, window_days, sector_codes=sector_codes)'
indices = []
start = 0
while True:
    idx = content.find(pattern, start)
    if idx == -1:
        break
    indices.append(idx)
    start = idx + 1

print(f"Found {len(indices)} occurrences:")
for i, idx in enumerate(indices):
    print(f"Occurrence {i+1} at byte {idx}:")
    print(content[idx:idx+200])
    print("---")
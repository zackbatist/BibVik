import json

path = '/home/zack/models/BibVik_output/_graph_state.json'
state = json.load(open(path))
pp = state.get('processed_papers', {})

# detection is stored directly as method_counts dict, not nested
bad = [k for k, v in pp.items() if v.get('detection', {}).get('llm_body_scan') is None]

print(f'Found {len(bad)} papers with LLM unavailable:')
for k in bad:
    print(' ', k)

if bad:
    for k in bad:
        del pp[k]
    json.dump(state, open(path, 'w'))
    print(f'Removed {len(bad)} entries from cache')
else:
    print('Cache is clean — nothing removed')

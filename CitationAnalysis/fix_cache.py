import json

path = '/home/zack/models/BibVik_output/_graph_state.json'
with open(path, encoding='utf-8') as f:
    state = json.load(f)
pp = state.get('processed_papers', {})

# detection is stored directly as method_counts dict, not nested
bad = [k for k, v in pp.items() if v.get('detection', {}).get('llm_body_scan') is None]

print(f'Found {len(bad)} papers with LLM unavailable:')
for k in bad:
    print(' ', k)

if bad:
    for k in bad:
        del pp[k]
    # ensure_ascii=False + encoding='utf-8' match utils.write_json's convention
    # used everywhere else in the pipeline — without these, non-ASCII author
    # names (Sindbæk, Gräslund, Cyrillic entries, etc.) would be silently
    # corrupted or escaped on write.
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f'Removed {len(bad)} entries from cache')
else:
    print('Cache is clean — nothing removed')
"""Bounded connected targets, never drawing-wide same-colour promotion."""
from collections import Counter, defaultdict
from shapely import STRtree
import semantic_layers


def group_targets(review, max_members=40, tolerance=0.01):
    records = review['records']
    identities = Counter((str(r.get('plan_id') or ''), str(r.get('entity_handle') or '')) for r in records)
    meanings = defaultdict(set)
    for entry in (review.get('summary', {}).get('source_legend') or {}).get('observations', []):
        meanings[(str(entry.get('plan_id') or ''), tuple(entry.get('signature', ())))].add(entry['category'])
    buckets, groups = defaultdict(list), []
    for row in review.get('unclassified', []):
        plan, handle = str(row.get('plan_id') or ''), str(row.get('entity_handle') or '')
        g = row.get('geometry')
        if not handle or identities[(plan, handle)] != 1 or g is None or g.is_empty:
            continue
        signature = semantic_layers.style_signature(row)
        if (g.geom_type not in {'LineString', 'MultiLineString'}
                or row.get('entity_type') in semantic_layers.TEXT_TYPES
                or len(meanings[(plan, signature)]) > 1):
            groups.append([row]); continue
        key = (plan, str(row.get('original_cad_layer') or row.get('cad_layer') or '0'),
               signature, str(row.get('source_year') or ''))
        buckets[key].append(row)
    for rows in buckets.values():
        tree = STRtree([r['geometry'] for r in rows])
        unused = set(range(len(rows)))
        for seed in range(len(rows)):
            if seed not in unused:
                continue
            unused.remove(seed)
            selected, pending = [seed], [seed]
            while pending and len(selected) < max_members:
                current = pending.pop()
                neighbours = sorted(int(i) for i in tree.query(rows[current]['geometry'],
                                          predicate='dwithin', distance=tolerance))
                for i in neighbours:
                    if i in unused:
                        unused.remove(i); selected.append(i); pending.append(i)
                        if len(selected) >= max_members:
                            break
            groups.append([rows[i] for i in sorted(selected)])
    return sorted(groups, key=lambda members: (-len(members), str(members[0].get('plan_id') or ''),
                                               str(members[0].get('entity_handle') or '')))

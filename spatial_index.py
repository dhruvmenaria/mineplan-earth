"""Plan-local spatial lookups; results retain deterministic input order."""
from collections import defaultdict
from shapely import STRtree


class RecordIndex:
    def __init__(self, records):
        self.records = records
        by_plan = defaultdict(list)
        for i, row in enumerate(records):
            geometry = row.get('geometry')
            if geometry is not None and not geometry.is_empty:
                by_plan[str(row.get('plan_id') or '')].append(i)
        self.indices = dict(by_plan)
        self.trees = {plan: STRtree([records[i]['geometry'] for i in ids])
                      for plan, ids in by_plan.items()}

    def near(self, plan, geometry, distance):
        plan = str(plan or '')
        if plan not in self.trees:
            return []
        ids = self.indices[plan]
        hits = self.trees[plan].query(geometry, predicate='dwithin', distance=distance)
        return [self.records[i] for i in sorted(ids[int(hit)] for hit in hits)]

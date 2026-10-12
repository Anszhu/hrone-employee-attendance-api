"""Walks MongoDB explain output recursively (no third-party imports). Used by the real-MongoDB explain tests (to
build failure messages) and by tests/explain_diagnostic_p52.py (to print what the planner did)."""
import json


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from walk(v)


def summarize(explain_output) -> dict:
    stages, index_names, lookup, plans = [], set(), [], []
    for n in walk(explain_output):
        if isinstance(n.get("stage"), str):
            stages.append(n["stage"])
        if isinstance(n.get("indexName"), str):
            index_names.add(n["indexName"])
        if "winningPlan" in n:
            plans.append("winningPlan")
        for key in ("$lookup", "$cursor"):
            if key in n and key == "$lookup":
                lookup.append({k: n[k] for k in n if k in ("totalDocsExamined", "totalKeysExamined", "collectionScans", "indexesUsed")})
        if any(k in n for k in ("collectionScans", "indexesUsed")):
            lookup.append({k: n[k] for k in ("collectionScans", "indexesUsed", "totalDocsExamined", "totalKeysExamined") if k in n})
    text = json.dumps(explain_output, default=str)
    return {"stages": stages, "index_names": sorted(index_names), "lookup_counters": lookup,
            "has_IXSCAN": "IXSCAN" in text, "has_COLLSCAN": "COLLSCAN" in text,
            "COLLSCAN_stages": [s for s in stages if s == "COLLSCAN"]}


def describe(explain_output) -> str:
    s = summarize(explain_output)
    return (f"IXSCAN={s['has_IXSCAN']} COLLSCAN={s['has_COLLSCAN']} stages={s['stages']} "
            f"indexNames={s['index_names']} lookup_counters={s['lookup_counters']}")

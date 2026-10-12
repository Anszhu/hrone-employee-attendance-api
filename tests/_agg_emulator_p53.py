"""Phase 5.3 test support: an EMULATION of the aggregation operators the late-leaderboard pipeline uses
($match with $gte/$lte/$gt/$eq/$type/$expr incl. dotted paths, $group, $lookup with a sub-pipeline, $unwind,
$setWindowFields with $rank, $sort, $project). Written from the documented MongoDB semantics - it is NOT MongoDB.
It lets the REAL pipeline object run without a server and catches logic mistakes; only the TEST_MONGO_URI
integration tests show whether MongoDB itself agrees. Anything not listed raises NotImplementedError so a pipeline
change cannot be silently mis-run. Reuses the Phase 5.2 emulator's expression evaluator, $group and $lookup (that
module is left untouched). Standalone: no third-party imports."""
from functools import cmp_to_key

from tests._agg_emulator_p52 import _group, _lookup, _num, _path, _project, bson_eq
from tests._agg_emulator_p52 import ev as _ev52


def _cmp_ok(actual, op, bound):
    """Type-bracketed comparison: a string bound only matches strings, a number bound only numbers."""
    if isinstance(bound, str):
        if not isinstance(actual, str):
            return False
    elif _num(bound):
        if not _num(actual):
            return False
    else:
        raise NotImplementedError(f"{op} with {type(bound).__name__}")
    return {"$gt": actual > bound, "$gte": actual >= bound, "$lt": actual < bound, "$lte": actual <= bound}[op]


def bson_type_name(v):
    """The $type aggregation operator's name for a value. A missing field and null are both reported as "missing"
    here (both are simply 'not a string' for these tests)."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "double"
    if isinstance(v, dict):
        return "object"
    return "missing"


def ev(expr, doc):
    """Expression evaluator: adds the $type operator (and routes $and / $eq through itself so it can nest) on top
    of the Phase 5.2 evaluator; everything else is delegated, so unsupported operators still raise."""
    if isinstance(expr, dict) and len(expr) == 1:
        (op, args), = expr.items()
        if op == "$type":
            return bson_type_name(ev(args, doc))
        if op == "$and":
            return all(bool(ev(a, doc)) for a in args)
        if op == "$eq":
            return bson_eq(ev(args[0], doc), ev(args[1], doc))
    return _ev52(expr, doc)


def _scalar_ok(actual, op, v):
    if op == "$eq":
        return bson_eq(actual, v)
    if op == "$type":
        if v != "string":
            raise NotImplementedError(f"$type {v}")
        return isinstance(actual, str)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        return _cmp_ok(actual, op, v)
    raise NotImplementedError(op)


def _field_ok(actual, cond):
    """MongoDB QUERY semantics: when the field is an ARRAY, $eq / $type match if ANY element matches (so
    {"$type": "string"} accepts ["Engineering"]) - that is exactly why the pipeline uses $expr instead."""
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, v in cond.items():
            if op in ("$eq", "$type") and isinstance(actual, list):
                ok = any(_scalar_ok(el, op, v) for el in actual)
            else:
                ok = _scalar_ok(actual, op, v)
            if not ok:
                return False
        return True
    if isinstance(actual, list) and not isinstance(cond, list):
        return any(bson_eq(el, cond) for el in actual)
    return bson_eq(actual, cond)


def _match(rows, spec):
    out = []
    for r in rows:
        ok = True
        for f, c in spec.items():
            if f == "$expr":
                ok = bool(ev(c, r))
            else:
                ok = _field_ok(_path(r, f), c)
            if not ok:
                break
        if ok:
            out.append(r)
    return out


def _unwind(rows, spec):
    if not (isinstance(spec, str) and spec.startswith("$")):
        raise NotImplementedError(f"$unwind {spec!r}")
    field, out = spec[1:], []
    for r in rows:
        v = r.get(field)
        if isinstance(v, list):
            out.extend({**r, field: item} for item in v)      # an empty array emits nothing (orphans are dropped)
        elif v is not None:
            out.append(r)
    return out


def _cmp_values(a, b):
    if isinstance(a, str) != isinstance(b, str):
        raise NotImplementedError("mixed-type sort keys (BSON cross-type ordering not emulated)")
    return (a > b) - (a < b)


def _sort(rows, spec):
    keys = list(spec.items())

    def cmp(x, y):
        for f, direction in keys:
            if direction not in (1, -1):
                raise NotImplementedError(f"$sort direction {direction!r}")
            c = _cmp_values(_path(x, f), _path(y, f))
            if c:
                return c * direction
        return 0
    return sorted(rows, key=cmp_to_key(cmp))


def _set_window_fields(rows, spec):
    """Only: no partitionBy, a single-field sortBy, and output {name: {"$rank": {}}} (competition ranking:
    rank = 1 + number of rows that sort strictly before the row)."""
    if set(spec) != {"sortBy", "output"} or len(spec["sortBy"]) != 1:
        raise NotImplementedError(f"$setWindowFields {spec!r}")
    ((field, direction),) = spec["sortBy"].items()
    ordered = _sort(rows, {field: direction})
    out = []
    for r in ordered:
        row = dict(r)
        for name, acc in spec["output"].items():
            if acc != {"$rank": {}}:
                raise NotImplementedError(f"window output {acc!r}")
            row[name] = 1 + sum(1 for o in ordered if _cmp_values(_path(o, field), _path(r, field)) * direction < 0)
        out.append(row)
    return out


def mini_aggregate(docs, pipeline, collections=None):
    rows = [dict(d) for d in docs]
    for stage in pipeline:
        (op, spec), = stage.items()
        if op == "$match":
            rows = _match(rows, spec)
        elif op == "$group":
            rows = _group(rows, spec)
        elif op == "$lookup":
            rows = _lookup(rows, spec, collections or {})
        elif op == "$unwind":
            rows = _unwind(rows, spec)
        elif op == "$setWindowFields":
            rows = _set_window_fields(rows, spec)
        elif op == "$sort":
            rows = _sort(rows, spec)
        elif op == "$project":
            rows = _project(rows, spec)
        else:
            raise NotImplementedError(op)
    return rows

"""Phase 5.4 test support: an EMULATION of the aggregation operators the department-trend pipeline uses
($match incl. $expr, $project, $lookup with a sub-pipeline, $facet, $group, $unwind, $replaceRoot, $addFields,
$setWindowFields with a $sum over a documents window, $sort; expressions $let, $map, $range, $dateAdd,
$dateToString, $isoDayOfWeek, $cond, $and, $in, $ifNull, $isNumber, $eq, $lte, $gt, $type, $sum, $add, $multiply,
$divide, $floor). Written from the documented MongoDB semantics - it is NOT MongoDB. It lets the REAL pipeline object
run without a server and catches logic mistakes; only the TEST_MONGO_URI integration tests show whether MongoDB itself
agrees. Anything not listed raises NotImplementedError, so a pipeline change cannot be silently mis-run.
Standalone: no third-party imports (and it does not import the earlier phases' emulators)."""
import math
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _path(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _truthy(v):
    return not (v is None or v is False or (_num(v) and v == 0))


def bson_eq(a, b):
    """Type-strict equality: true != 1, numbers compare across int/float, missing == null."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _num(a) and _num(b):
        return a == b
    return type(a) is type(b) and a == b


def bson_type_name(v):
    if v is None:
        return "missing"          # missing and null are both simply "not a string" for these tests
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
    if isinstance(v, datetime):
        return "date"
    return "other"


def _order(v):
    """MongoDB's cross-type comparison order for the few types used here: null < numbers < strings."""
    if v is None:
        return 0
    if _num(v):
        return 1
    if isinstance(v, str):
        return 2
    raise NotImplementedError(f"ordering of {type(v).__name__}")


def _cmp(a, b):
    ta, tb = _order(a), _order(b)
    if ta != tb:
        return (ta > tb) - (ta < tb)
    return (a > b) - (a < b)


def ev(expr, doc, vars_=None):
    vars_ = vars_ or {}
    if isinstance(expr, str):
        if expr.startswith("$$"):
            name, _, rest = expr[2:].partition(".")
            v = vars_[name]
            return _path(v, rest) if rest else v
        if expr.startswith("$"):
            return _path(doc, expr[1:])
        return expr
    if isinstance(expr, list):
        return [ev(e, doc, vars_) for e in expr]
    if not isinstance(expr, dict):
        return expr
    if len(expr) == 1 and next(iter(expr)).startswith("$"):
        return _operator(*next(iter(expr.items())), doc, vars_)
    if any(k.startswith("$") for k in expr):
        raise NotImplementedError(f"mixed object expression {expr!r}")
    return {k: ev(v, doc, vars_) for k, v in expr.items()}      # an object expression: every field is an expression


def _operator(op, args, doc, vars_):
    e = lambda x: ev(x, doc, vars_)             # noqa: E731
    if op == "$ifNull":
        v = e(args[0])
        return e(args[1]) if v is None else v
    if op == "$eq":
        return bson_eq(e(args[0]), e(args[1]))
    if op == "$lte":
        return _cmp(e(args[0]), e(args[1])) <= 0
    if op == "$gt":
        return _cmp(e(args[0]), e(args[1])) > 0
    if op == "$and":
        return all(_truthy(e(a)) for a in args)            # short-circuits, like MongoDB
    if op == "$in":
        needle, hay = e(args[0]), e(args[1])
        if not isinstance(hay, list):
            raise ValueError("$in needs an array as its second argument")
        return any(bson_eq(needle, h) for h in hay)
    if op == "$cond":
        return e(args[1]) if _truthy(e(args[0])) else e(args[2])
    if op == "$isNumber":
        return _num(e(args))
    if op == "$type":
        return bson_type_name(e(args))
    if op == "$let":
        new = dict(vars_)
        for k, v in args["vars"].items():
            new[k] = ev(v, doc, vars_)
        return ev(args["in"], doc, new)
    if op == "$map":
        items = e(args["input"])
        if items is None:
            return None
        return [ev(args["in"], doc, {**vars_, args["as"]: it}) for it in items]
    if op == "$range":
        lo, hi = e(args)
        return list(range(lo, hi))
    if op == "$dateAdd":
        if args["unit"] != "day":
            raise NotImplementedError("$dateAdd unit")
        return e(args["startDate"]) + timedelta(days=e(args["amount"]))      # UTC, no time zone / DST involved
    if op == "$dateToString":
        if args["format"] != "%Y-%m-%d" or "timezone" in args:
            raise NotImplementedError("$dateToString options")
        d = e(args["date"]).astimezone(timezone.utc)
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if op == "$isoDayOfWeek":
        return e(args).isoweekday()
    if op == "$sum":                                   # expression form: sums an array's numeric elements
        v = e(args)
        if isinstance(v, list):
            return sum(x for x in v if _num(x))
        return v if _num(v) else 0
    if op == "$add":
        vals = [e(a) for a in args]
        return None if any(v is None for v in vals) else sum(vals)
    if op == "$multiply":
        vals = [e(a) for a in args]
        if any(v is None for v in vals):
            return None
        out = 1
        for v in vals:
            out *= v
        return out
    if op == "$divide":
        a, b = e(args[0]), e(args[1])
        return None if a is None or b is None else a / b       # always a double, as in MongoDB
    if op == "$floor":
        v = e(args)
        return None if v is None else (float(math.floor(v)) if isinstance(v, float) else v)   # keeps the input type
    raise NotImplementedError(op)


# ---- query-language $match (array fields match if ANY element matches, like MongoDB) ----
def _scalar_ok(actual, op, v):
    if op == "$eq":
        return bson_eq(actual, v)
    if op == "$type":
        if v != "string":
            raise NotImplementedError(f"$type {v}")
        return isinstance(actual, str)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        if isinstance(v, str):                          # type bracketing: a string bound only matches strings
            if not isinstance(actual, str):
                return False
        elif _num(v):
            if not _num(actual):
                return False
        else:
            raise NotImplementedError(f"{op} with {type(v).__name__}")
        return {"$gt": actual > v, "$gte": actual >= v, "$lt": actual < v, "$lte": actual <= v}[op]
    raise NotImplementedError(op)


def _field_ok(actual, cond):
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


def match_docs(rows, spec):
    out = []
    for r in rows:
        ok = True
        for f, c in spec.items():
            ok = bool(ev(c, r)) if f == "$expr" else _field_ok(_path(r, f), c)
            if not ok:
                break
        if ok:
            out.append(r)
    return out


def _project(rows, spec):
    out = []
    for d in rows:
        row = {}
        if spec.get("_id", 1) != 0 and "_id" in d:
            row["_id"] = d["_id"]
        for k, v in spec.items():
            if k == "_id":
                continue
            if v == 1 and not isinstance(v, bool):
                if k in d:
                    row[k] = d[k]
            elif v == 0 and not isinstance(v, bool):
                raise NotImplementedError("exclusion mixed with inclusion")
            else:
                val = ev(v, d)
                if val is not None or isinstance(v, dict):      # a missing path drops the field; null results stay
                    row[k] = val
        out.append(row)
    return out


def _add_fields(rows, spec):
    return [{**d, **{k: ev(v, d) for k, v in spec.items()}} for d in rows]


def _group(rows, spec):
    groups = {}
    for d in rows:
        key = ev(spec["_id"], d)
        g = groups.setdefault(key, {"_id": key})
        for name, acc in spec.items():
            if name == "_id":
                continue
            (op, expr), = acc.items()
            if op != "$sum":
                raise NotImplementedError(op)
            v = ev(expr, d)
            g[name] = g.get(name, 0) + (v if _num(v) else 0)
    return list(groups.values())


def _unwind(rows, spec):
    if not (isinstance(spec, str) and spec.startswith("$")):
        raise NotImplementedError(f"$unwind {spec!r}")
    field, out = spec[1:], []
    for r in rows:
        v = r.get(field)
        if isinstance(v, list):
            out.extend({**r, field: item} for item in v)        # an empty array emits nothing
        elif v is not None:
            out.append(r)
    return out


def _sort(rows, spec):
    keys = list(spec.items())

    def cmp(x, y):
        for f, direction in keys:
            c = _cmp(_path(x, f), _path(y, f))
            if c:
                return c * direction
        return 0
    return sorted(rows, key=cmp_to_key(cmp))


def _set_window_fields(rows, spec):
    """Only: no partitionBy, a single-field sortBy, outputs {name: {"$sum": expr, "window": {"documents": [lo, hi]}}}."""
    if set(spec) != {"sortBy", "output"} or len(spec["sortBy"]) != 1:
        raise NotImplementedError(f"$setWindowFields {spec!r}")
    ((field, direction),) = spec["sortBy"].items()
    ordered = _sort(rows, {field: direction})
    out = []
    for i, r in enumerate(ordered):
        row = dict(r)
        for name, acc in spec["output"].items():
            if set(acc) != {"$sum", "window"} or set(acc["window"]) != {"documents"}:
                raise NotImplementedError(f"window output {acc!r}")
            lo, hi = acc["window"]["documents"]
            window = ordered[max(0, i + lo): max(0, i + hi + 1)]       # clipped at the start of the partition
            row[name] = sum(v for v in (ev(acc["$sum"], w) for w in window) if _num(v))
        out.append(row)
    return out


def _lookup(rows, spec, collections):
    foreign = collections[spec["from"]]
    out = []
    for d in rows:
        local = d.get(spec["localField"])
        matched = [dict(f) for f in foreign if bson_eq(f.get(spec["foreignField"]), local)]
        out.append({**d, spec["as"]: mini_aggregate(matched, spec.get("pipeline", []), collections)})
    return out


def mini_aggregate(docs, pipeline, collections=None):
    rows = [dict(d) for d in docs]
    for stage in pipeline:
        (op, spec), = stage.items()
        if op == "$match":
            rows = match_docs(rows, spec)
        elif op == "$project":
            rows = _project(rows, spec)
        elif op == "$addFields":
            rows = _add_fields(rows, spec)
        elif op == "$lookup":
            rows = _lookup(rows, spec, collections or {})
        elif op == "$facet":                                 # exactly one output document, even for empty input
            rows = [{name: mini_aggregate(rows, sub, collections) for name, sub in spec.items()}]
        elif op == "$group":
            rows = _group(rows, spec)
        elif op == "$unwind":
            rows = _unwind(rows, spec)
        elif op == "$replaceRoot":
            rows = [dict(ev(spec["newRoot"], r)) for r in rows]
        elif op == "$setWindowFields":
            rows = _set_window_fields(rows, spec)
        elif op == "$sort":
            rows = _sort(rows, spec)
        else:
            raise NotImplementedError(op)
    return rows

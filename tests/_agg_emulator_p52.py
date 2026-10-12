"""Phase 5.2 test support: an EMULATION of the aggregation operators the department-summary pipeline uses
($match, $lookup with a sub-pipeline, $group, $sort, $project; $map, $sum (array form), $cond, $in, $isNumber,
$toLong, $floor, ...). It is written from the documented MongoDB semantics - it is NOT MongoDB. It lets the REAL
pipeline object run in environments without a server and catches logic mistakes; only the TEST_MONGO_URI
integration tests show whether MongoDB itself agrees. Floats are Python floats, i.e. IEEE-754 doubles like
BSON doubles. Unsupported operators raise NotImplementedError so a pipeline change cannot be silently mis-run.
Standalone module (no third-party imports); tests/_agg_emulator.py (Phase 5.1) is left untouched."""


def _path(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _truthy(v):
    return not (v is None or v is False or (_num(v) and v == 0))


def bson_eq(a, b):
    """Type-strict equality: true != 1, numbers compare across int/float, missing == null."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _num(a) and _num(b):
        return a == b
    return type(a) is type(b) and a == b if a is not None and b is not None else a is None and b is None


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
    if len(expr) != 1 or not next(iter(expr)).startswith("$"):
        raise NotImplementedError(f"object expression {expr!r}")
    op, args = next(iter(expr.items()))
    if op == "$ifNull":
        v = ev(args[0], doc, vars_)
        return ev(args[1], doc, vars_) if v is None else v
    if op == "$eq":
        return bson_eq(ev(args[0], doc, vars_), ev(args[1], doc, vars_))
    if op == "$ne":
        return not bson_eq(ev(args[0], doc, vars_), ev(args[1], doc, vars_))
    if op == "$gt":
        a, b = ev(args[0], doc, vars_), ev(args[1], doc, vars_)
        if not (_num(a) and _num(b)):
            raise NotImplementedError("$gt on non-numbers (BSON cross-type ordering not emulated)")
        return a > b
    if op == "$and":
        return all(_truthy(ev(a, doc, vars_)) for a in args)
    if op == "$not":
        return not _truthy(ev(args[0], doc, vars_))
    if op == "$in":
        haystack = ev(args[1], doc, vars_)
        if not isinstance(haystack, list):
            raise ValueError("$in needs an array as its second argument")
        needle = ev(args[0], doc, vars_)
        return any(bson_eq(needle, h) for h in haystack)
    if op == "$cond":
        return ev(args[1], doc, vars_) if _truthy(ev(args[0], doc, vars_)) else ev(args[2], doc, vars_)
    if op == "$isNumber":
        return _num(ev(args, doc, vars_))
    if op == "$map":
        items = ev(args["input"], doc, vars_)
        if items is None:
            return None
        return [ev(args["in"], doc, {**vars_, args["as"]: it}) for it in items]
    if op == "$sum":                                   # expression form: sums an array's numeric elements
        v = ev(args, doc, vars_)
        if isinstance(v, list):
            return sum(x for x in v if _num(x))
        return v if _num(v) else 0
    if op == "$add":
        vals = [ev(a, doc, vars_) for a in args]
        return None if any(v is None for v in vals) else sum(vals)
    if op == "$multiply":
        vals = [ev(a, doc, vars_) for a in args]
        if any(v is None for v in vals):
            return None
        out = 1
        for v in vals:
            out *= v
        return out
    if op == "$floor":
        v = ev(args, doc, vars_)
        if v is None:
            return None
        import math
        return math.floor(v)
    if op == "$toLong":
        v = ev(args, doc, vars_)
        return None if v is None else int(v)           # truncation toward zero; input here is already floored
    raise NotImplementedError(op)


def _field_ok(actual, cond):
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, v in cond.items():
            if op == "$eq" and not bson_eq(actual, v):
                return False
            elif op == "$ne" and bson_eq(actual, v):
                return False
            elif op == "$type":
                if v != "string":
                    raise NotImplementedError(f"$type {v}")
                if not isinstance(actual, str):
                    return False
            elif op in ("$lte", "$gte"):
                if isinstance(v, str):
                    ok = isinstance(actual, str) and (actual <= v if op == "$lte" else actual >= v)   # type bracketing
                else:
                    raise NotImplementedError("numeric range")
                if not ok:
                    return False
            elif op not in ("$eq", "$ne"):
                raise NotImplementedError(op)
        return True
    return bson_eq(actual, cond)


def _match(docs, spec):
    return [d for d in docs if all(_field_ok(d.get(f), c) for f, c in spec.items())]


def _project(docs, spec):
    out = []
    for d in docs:
        row, include_mode = {}, any(v == 1 or isinstance(v, str) for k, v in spec.items() if k != "_id")
        if include_mode:
            if spec.get("_id", 1) != 0 and "_id" in d:
                row["_id"] = d["_id"]
            for k, v in spec.items():
                if k == "_id":
                    continue
                if v == 1:
                    if k in d:
                        row[k] = d[k]
                elif isinstance(v, str) and v.startswith("$"):
                    row[k] = _path(d, v[1:])
                else:
                    raise NotImplementedError(f"$project {k}: {v!r}")
        else:
            row = {k: v for k, v in d.items() if spec.get(k, 1) != 0}
        out.append(row)
    return out


def _lookup(docs, spec, collections):
    foreign = collections[spec["from"]]
    out = []
    for d in docs:
        local = d.get(spec["localField"])
        matched = [dict(f) for f in foreign if bson_eq(f.get(spec["foreignField"]), local)]
        out.append({**d, spec["as"]: mini_aggregate(matched, spec.get("pipeline", []), collections)})
    return out


def _group(docs, spec):
    groups = {}
    for d in docs:
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


def mini_aggregate(docs, pipeline, collections=None):
    rows = list(docs)
    for stage in pipeline:
        (op, spec), = stage.items()
        if op == "$match":
            rows = _match(rows, spec)
        elif op == "$lookup":
            rows = _lookup(rows, spec, collections or {})
        elif op == "$group":
            rows = _group(rows, spec)
        elif op == "$sort":
            if spec != {"_id": 1}:
                raise NotImplementedError(f"$sort {spec}")
            rows.sort(key=lambda r: r["_id"])          # str ordering by code point == MongoDB's default binary order
        elif op == "$project":
            rows = _project(rows, spec)
        else:
            raise NotImplementedError(op)
    return rows

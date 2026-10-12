"""Phase 5.1 test support: a TINY INTERPRETER for the aggregation operators the monthly pipeline uses.

This is an EMULATION written from the documented MongoDB semantics - it is NOT MongoDB. It exists so the
real pipeline object (built by app.main.employee_monthly_pipeline) gets EXECUTED in environments without a
server, which catches logic mistakes in how the expressions are put together. Whether MongoDB itself agrees
is checked only by the TEST_MONGO_URI integration tests. Unsupported operators raise NotImplementedError, so
a pipeline change that needs new operators cannot be silently mis-evaluated here."""

def _truthy(v):
    return not (v is None or v is False or v == 0)


def evaluate(expr, doc):
    if isinstance(expr, str):
        return doc.get(expr[1:]) if expr.startswith("$") else expr       # missing field -> None (MongoDB: missing)
    if isinstance(expr, list):
        return [evaluate(e, doc) for e in expr]
    if isinstance(expr, dict):
        if len(expr) != 1 or not next(iter(expr)).startswith("$"):
            raise NotImplementedError(f"object expression {expr!r}")
        op, args = next(iter(expr.items()))
        if op == "$ifNull":
            value = evaluate(args[0], doc)
            return evaluate(args[1], doc) if value is None else value        # null and missing both fall back
        if op == "$eq":
            return evaluate(args[0], doc) == evaluate(args[1], doc)
        if op == "$gt":
            a, b = evaluate(args[0], doc), evaluate(args[1], doc)
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
                raise NotImplementedError("$gt on non-numbers (BSON cross-type ordering not emulated)")
            return a > b
        if op == "$and":
            return all(_truthy(evaluate(a, doc)) for a in args)
        if op == "$not":
            return not _truthy(evaluate(args[0], doc))
        if op == "$in":
            return evaluate(args[0], doc) in evaluate(args[1], doc)
        if op == "$cond":
            return evaluate(args[1], doc) if _truthy(evaluate(args[0], doc)) else evaluate(args[2], doc)
        raise NotImplementedError(op)
    return expr                                                              # numbers, bools, None


def _match_stage(docs, spec):
    out = []
    for d in docs:
        ok = True
        for field, cond in spec.items():
            actual = d.get(field)
            if isinstance(cond, dict):
                for op, v in cond.items():
                    if actual is None or isinstance(actual, bool):
                        ok = False
                    elif op == "$gte":
                        ok = ok and actual >= v
                    elif op == "$lte":
                        ok = ok and actual <= v
                    else:
                        raise NotImplementedError(op)
            else:
                ok = ok and actual == cond
        if ok:
            out.append(d)
    return out


def _group_stage(docs, spec):
    if spec["_id"] is not None:
        raise NotImplementedError("$group by key")
    if not docs:
        return []                      # MongoDB: $group over ZERO documents emits NO document
    row = {"_id": None}
    for name, acc in spec.items():
        if name == "_id":
            continue
        (op, expr), = acc.items()
        if op != "$sum":
            raise NotImplementedError(op)
        total = 0
        for d in docs:
            v = evaluate(expr, d)
            if isinstance(v, (int, float)) and not isinstance(v, bool):      # $sum ignores non-numbers
                total += v
        row[name] = total
    return [row]


def mini_aggregate(docs, pipeline):
    rows = list(docs)
    for stage in pipeline:
        (op, spec), = stage.items()
        if op == "$match":
            rows = _match_stage(rows, spec)
        elif op == "$group":
            rows = _group_stage(rows, spec)
        else:
            raise NotImplementedError(op)
    return rows

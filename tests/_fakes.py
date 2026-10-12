"""In-memory fakes for Phase 3 tests. These are MOCKS: they model the few MongoDB behaviours the
endpoint code relies on (equality/range filters, sort, skip/limit, conditional update, ping,
create_index) and prove the code's branching and query SHAPE. They prove nothing about MongoDB's real
concurrency, index or planner behaviour - that is what the TEST_MONGO_URI integration tests are for."""
import copy
import threading

from pymongo.errors import DuplicateKeyError


def _aware(v):
    """Naive datetimes compare as UTC (BSON dates have no zone), so naive and aware values mix safely."""
    from datetime import datetime, timezone
    return v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v


def _cond_ok(actual, cond):
    if isinstance(cond, dict) and cond and all(str(k).startswith("$") for k in cond):
        if actual is None:          # like MongoDB: range operators never match null / missing
            return False
        actual = _aware(actual)
        for op, v in cond.items():
            v = _aware(v)
            if op == "$gte" and not actual >= v:
                return False
            if op == "$lte" and not actual <= v:
                return False
            if op == "$gt" and not actual > v:
                return False
            if op == "$lt" and not actual < v:
                return False
            if op not in ("$gte", "$lte", "$gt", "$lt"):
                raise NotImplementedError(op)
        return True
    return actual == cond           # None matches null AND missing, as in MongoDB


def matches(doc, flt):
    return all(_cond_ok(doc.get(k), v) for k, v in (flt or {}).items())


def project(doc, projection):
    doc = dict(doc)
    if not projection:
        return doc
    include = [k for k, v in projection.items() if v and k != "_id"]
    if include:
        out = {k: doc[k] for k in include if k in doc}
        if projection.get("_id", 1):
            out["_id"] = doc.get("_id")
        return out
    for k, v in projection.items():
        if not v:
            doc.pop(k, None)
    return doc


def _normalize_sort(key, direction=None):
    return list(key) if isinstance(key, list) else [(key, 1 if direction is None else direction)]


def sort_docs(docs, spec):
    for key, direction in reversed(spec):
        docs.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction < 0)
    return docs


class FakeCursor:
    def __init__(self, docs, projection):
        self.docs, self.projection = list(docs), projection
        self.sort_spec = None
        self.skip_n = 0
        self.limit_n = None

    def sort(self, key, direction=None):
        self.sort_spec = _normalize_sort(key, direction)
        sort_docs(self.docs, self.sort_spec)
        return self

    def skip(self, n):
        self.skip_n = n
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def __iter__(self):
        docs = self.docs[self.skip_n:]
        if self.limit_n:
            docs = docs[: self.limit_n]
        return iter([project(d, self.projection) for d in docs])


class FakeCollection:
    def __init__(self, unique=None):
        self.unique = unique
        self.docs = []
        self.calls = []
        self.count_filters = []
        self.last_cursor = None
        self.last_update = None          # (filter, update) of the last find_one_and_update
        self.before_update = None        # callable run just before the conditional update matches
        self.force_error = None
        self.created_indexes = []
        self.index_error = None
        self._n = 0
        self._lock = threading.Lock()

    def insert_one(self, doc):
        self.calls.append("insert_one")
        if self.force_error:
            raise self.force_error
        with self._lock:
            if self.unique and any(all(d.get(k) == doc.get(k) for k in self.unique) for d in self.docs):
                raise DuplicateKeyError("E11000 duplicate key error (fake unique check)")
            self._n += 1
            doc["_id"] = f"oid{self._n}"
            self.docs.append(copy.deepcopy(doc))

    def find_one(self, flt=None, projection=None, sort=None):
        self.calls.append("find_one")
        if self.force_error:
            raise self.force_error
        found = [d for d in self.docs if matches(d, flt)]
        if sort:
            sort_docs(found, _normalize_sort(sort))
        return project(found[0], projection) if found else None

    def find_one_and_update(self, flt, update, return_document=False):
        self.calls.append("find_one_and_update")
        self.last_update = (copy.deepcopy(flt), copy.deepcopy(update))
        if self.before_update:
            self.before_update()
        with self._lock:                 # the match-and-modify step is atomic, like a single-document update
            for d in self.docs:
                if matches(d, flt):
                    before = copy.deepcopy(d)
                    d.update(update["$set"])
                    return copy.deepcopy(d) if return_document else before
        return None

    def count_documents(self, flt):
        self.calls.append("count_documents")
        self.count_filters.append(copy.deepcopy(flt))
        return sum(1 for d in self.docs if matches(d, flt))

    def find(self, flt=None, projection=None):
        self.calls.append("find")
        self.last_cursor = FakeCursor([d for d in self.docs if matches(d, flt)], projection)
        return self.last_cursor

    def create_index(self, keys, **opts):
        self.calls.append("create_index")
        if self.index_error:
            raise self.index_error
        self.created_indexes.append((list(keys), dict(opts)))
        return "_".join(f"{k}_{d}" for k, d in keys)


class FakeDB:
    def __init__(self):
        self.employees = FakeCollection(unique=("emp_code",))
        self.attendance_logs = FakeCollection(unique=("emp_code", "date"))
        self.ping_error = None
        self.pings = 0

    def __getitem__(self, name):
        return getattr(self, name)

    def command(self, name):
        assert name == "ping"
        self.pings += 1
        if self.ping_error:
            raise self.ping_error
        return {"ok": 1.0}

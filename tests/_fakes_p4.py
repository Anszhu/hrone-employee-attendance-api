"""Phase 4 additions to the in-memory fake (tests/_fakes.py is left untouched).

Adds what the PATCH write needs: the {"$size": n} filter operator and a {"$push": ...} update next to
{"$set": ...}. Like the rest of the fakes this is a MOCK: it proves the code's query SHAPE and branching,
not MongoDB's atomicity - that is what the TEST_MONGO_URI integration tests check."""
import copy

from tests._fakes import FakeCollection, FakeDB, _cond_ok


def _match_value_p4(actual, cond):
    if isinstance(cond, dict) and set(cond) == {"$size"}:
        return isinstance(actual, list) and len(actual) == cond["$size"]
    return _cond_ok(actual, cond)


def matches_p4(doc, flt):
    return all(_match_value_p4(doc.get(k), v) for k, v in (flt or {}).items())


class FakeCollectionP4(FakeCollection):
    def find_one_and_update(self, flt, update, return_document=False):
        self.calls.append("find_one_and_update")
        self.last_update = (copy.deepcopy(flt), copy.deepcopy(update))
        if self.before_update:
            self.before_update()
        with self._lock:
            for d in self.docs:
                if matches_p4(d, flt):
                    before = copy.deepcopy(d)
                    for k, v in update.get("$set", {}).items():
                        d[k] = copy.deepcopy(v)
                    for k, v in update.get("$push", {}).items():
                        if not isinstance(d.get(k), list):
                            raise RuntimeError("$push on a non-array field (MongoDB would raise WriteError)")
                        d[k].append(copy.deepcopy(v))
                    unknown = set(update) - {"$set", "$push"}
                    if unknown:
                        raise NotImplementedError(unknown)
                    return copy.deepcopy(d) if return_document else before
        return None


class FakeDBP4(FakeDB):
    def __init__(self):
        super().__init__()
        self.attendance_logs = FakeCollectionP4(unique=("emp_code", "date"))

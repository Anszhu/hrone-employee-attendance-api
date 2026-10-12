"""Phase 5.2 fake database: employees.aggregate() runs the pipeline with the EMULATOR in
tests/_agg_emulator_p52.py, resolving $lookup against the attendance_logs documents. A MOCK: it proves routing,
error mapping and that ONE aggregation (no find) is used - not MongoDB behaviour."""
from tests._agg_emulator_p52 import mini_aggregate
from tests._fakes import FakeCollection, FakeDB


class FakeEmployeesP52(FakeCollection):
    def __init__(self, resolver, **kw):
        super().__init__(**kw)
        self._resolver = resolver
        self.last_pipeline = None

    def aggregate(self, pipeline):
        self.calls.append("aggregate")
        self.last_pipeline = pipeline
        return iter(mini_aggregate(self.docs, pipeline, {"attendance_logs": self._resolver("attendance_logs")}))


class FakeDBP52(FakeDB):
    def __init__(self):
        super().__init__()
        self.employees = FakeEmployeesP52(lambda name: getattr(self, name).docs, unique=("emp_code",))

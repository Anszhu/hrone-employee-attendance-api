"""Phase 5.3 fake database: attendance_logs.aggregate() runs the pipeline with the EMULATOR in
tests/_agg_emulator_p53.py, resolving $lookup against the employees documents; db.command() records an explain
request. A MOCK: it proves routing, error mapping, "one aggregation, no find" and that explain reuses the builder -
not MongoDB behaviour."""
from tests._agg_emulator_p53 import mini_aggregate
from tests._fakes import FakeCollection, FakeDB


class FakeLogsP53(FakeCollection):
    def __init__(self, resolver, **kw):
        super().__init__(**kw)
        self._resolver = resolver

    def aggregate(self, pipeline):
        self.calls.append("aggregate")
        self.last_pipeline = pipeline
        return iter(mini_aggregate(self.docs, pipeline, {"employees": self._resolver("employees")}))


class FakeDBP53(FakeDB):
    def __init__(self):
        super().__init__()
        self.attendance_logs = FakeLogsP53(lambda name: getattr(self, name).docs, unique=("emp_code", "date"))
        self.commands = []

    def command(self, cmd):
        self.commands.append(cmd)
        return {"queryPlanner": {"winningPlan": {"stage": "FAKE"}}, "executionStats": {"nReturned": 0}}

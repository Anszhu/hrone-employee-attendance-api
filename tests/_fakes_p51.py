"""Phase 5.1 fake collection: adds aggregate() backed by the EMULATOR in tests/_agg_emulator.py
(an emulation of MongoDB semantics, not MongoDB)."""
from tests._agg_emulator import mini_aggregate
from tests._fakes import FakeCollection, FakeDB


class FakeCollectionP51(FakeCollection):
    def aggregate(self, pipeline):
        self.calls.append("aggregate")
        self.last_pipeline = pipeline
        return iter(mini_aggregate(self.docs, pipeline))


class FakeDBP51(FakeDB):
    def __init__(self):
        super().__init__()
        self.attendance_logs = FakeCollectionP51(unique=("emp_code", "date"))

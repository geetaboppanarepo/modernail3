"""Fixtures shared by every test in this folder.

A unit test here never calls the model, never downloads anything, and never
touches the real state/ or .cache/ folders:

    fresh_store   says a test changes orders; every test already gets its own
                  freshly seeded orders.db, through tmp_state
    tmp_state     state/ and .cache/ redirected to a temp folder (always on)
    fake_llm      a scripted stand-in for llm.complete (see fakes.py)
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

from ami import observe, store
from fakes import FakeLLM


@pytest.fixture
def fresh_store(tmp_state):
    """The fixture orders, in this test's own state/orders.db. tmp_state
    already gives every test a new one; asking for this says the test
    reads or changes orders, and returns the database path."""
    return store.path()


REAL_STATE = Path(__file__).resolve().parent.parent / "state"

# Prevention, under the check below: tmp_state removes these per test so the
# temp ROOT is used, and if a test ever undoes tmp_state's patches, this is
# what it falls back to — a throwaway folder, never state/.
_FALLBACK = Path(tempfile.mkdtemp(prefix="ami-tests-"))
os.environ["AMI_ORDERS_DB"] = str(_FALLBACK / "orders.db")
os.environ["AMI_APPROVALS_DB"] = str(_FALLBACK / "approvals.db")


@pytest.fixture(autouse=True, scope="session")
def real_state_untouched():
    """Fail the run if any test wrote to the real state/ databases, e.g. a
    test that undid tmp_state's patches and fell back to the default path."""
    def snap():
        return {n: (REAL_STATE / n).stat().st_mtime_ns if (REAL_STATE / n).exists() else None
                for n in ("orders.db", "approvals.db")}
    before = snap()
    yield
    assert snap() == before, "a test touched the real state/orders.db or approvals.db"


@pytest.fixture(autouse=True)
def tmp_state(monkeypatch, tmp_path):
    """Point every on-disk path at a temp folder, for every test.

    observe writes state/trace.jsonl, knowledge builds .cache/chroma, and
    (Stage 2) LongTermMemory writes state/customers.json and the approval
    workflow writes state/approvals.db and the order store state/orders.db. Each module has
    already imported ROOT, so the paths are patched where they are used.
    """
    monkeypatch.setattr("ami.ROOT", tmp_path)
    monkeypatch.setattr("ami.observe.ROOT", tmp_path)
    monkeypatch.setattr("ami.observe.LOGFILE", tmp_path / "state" / "trace.jsonl")
    monkeypatch.setattr("ami.knowledge.STORE", tmp_path / ".cache" / "chroma")
    monkeypatch.setattr("ami.memory.ROOT", tmp_path, raising=False)
    monkeypatch.setattr("ami.approvals.ROOT", tmp_path)       # state/approvals.db
    monkeypatch.delenv("AMI_APPROVALS_DB", raising=False)
    monkeypatch.setattr("ami.store.ROOT", tmp_path)           # state/orders.db
    monkeypatch.setattr("ami.store.PATH", None)
    monkeypatch.delenv("AMI_ORDERS_DB", raising=False)
    observe.EVENTS.clear()
    monkeypatch.setattr("ami.observe.SEQ", 0)
    return tmp_path


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace llm.complete everywhere it was imported with a scripted fake."""
    fake = FakeLLM()
    for name, module in list(sys.modules.items()):
        if name == "ami.llm" or (name.startswith("ami.") and hasattr(module, "complete")):
            monkeypatch.setattr(module, "complete", fake)
    return fake


# --------------------------------------------------------------------------
# Exit without native teardown
# --------------------------------------------------------------------------
# ami.tools imports onnxruntime (knowledge -> embedder). onnxruntime 1.30's
# telemetry uploader runs on a native thread; when an upload's HTTP response
# lands while the interpreter is tearing down, it locks a destroyed mutex
# and the process aborts — "libc++abi: ... recursive_mutex lock failed",
# exit 134 — AFTER every test has passed (macOS crash reports put it in
# Microsoft::Applications::Events::HttpResponseDecoder). It is not the
# tests: `python -c "import onnxruntime; time.sleep(10)"` does it too, about
# one run in four. So once pytest has reported, leave with its status and
# skip native teardown. AMI_TEST_NORMAL_EXIT=1 turns this off (e.g. for a
# coverage tool that writes its data at exit).

def pytest_sessionfinish(session, exitstatus):
    session.config._ami_exitstatus = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if os.environ.get("AMI_TEST_NORMAL_EXIT") or not hasattr(config, "_ami_exitstatus"):
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(config._ami_exitstatus)

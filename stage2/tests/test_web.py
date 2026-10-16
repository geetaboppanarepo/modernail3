"""web.py over real HTTP: the chat contract and the reviewer endpoints.

A real ThreadingHTTPServer on an ephemeral port, real cookies, a scripted
model. State, sessions and the approvals database are all in a temp folder.

Pins:
- /approvals refuses a missing, wrong, or unconfigured reviewer token
- a reviewer can list, read, approve and reject; a second decision is
  refused and the action runs once; bad ids and illegal moves get 4xx
- a customer sees their own approval request, never another customer's
- /chat reports the real approval status from the database, separately
  from an ESC ticket, and a turn with nothing protected is unchanged
"""

import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from ami import ROOT as STAGE, approvals, tools
from ami.memory import LongTermMemory
from fakes import Reply, tool_call

TOKEN = "test-reviewer-token"
HEADPHONES = "112-1111111-1111111"          # $348, delivered — over the limit
KINDLE = "112-3333333-3333333"              # $149.99, preparing — under it


@pytest.fixture
def web(tmp_state, monkeypatch, fresh_store):
    """web.py, imported against temp state. web.py reads ui/ and state/ at
    import, so ui/ is copied into the temp root and nothing real is opened."""
    if "web" not in sys.modules:
        shutil.copytree(STAGE / "ui", tmp_state / "ui", dirs_exist_ok=True)
    import web
    monkeypatch.setattr(web, "SESSIONS", {})
    monkeypatch.setattr(web, "SESSIONS_FILE", tmp_state / "state" / "sessions.json")
    monkeypatch.setattr(web, "FEEDBACK_FILE", tmp_state / "state" / "feedback.jsonl")
    monkeypatch.setattr(web, "LONGTERM", LongTermMemory(tmp_state / "state" / "customers.json"))
    monkeypatch.setenv("AMI_REVIEWER_TOKEN", TOKEN)
    return web


@pytest.fixture
def server(web):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def ran(monkeypatch):
    calls, real = [], tools.run

    def spy(name, args, **kw):
        calls.append(name)
        return real(name, args, **kw)
    monkeypatch.setattr(tools, "run", spy)
    return calls


class Client:
    """One browser: its own cookie jar, so its own session."""

    def __init__(self, base, token=None):
        self.base, self.token = base, token
        # ProxyHandler({}): never ask macOS for system proxy settings. That
        # lookup (_scproxy) loads CFNetwork, whose threads aborted the
        # interpreter at exit in ~30% of full-suite runs; localhost needs no proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body or {}).encode() if method == "POST" else None)
        if token or self.token:
            req.add_header("X-Reviewer-Token", token or self.token)
        try:
            with self.opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def chat(self, text):
        return self.call("POST", "/chat", {"message": text, "planner": "react"})


def args(**kw):
    return {"order_id": HEADPHONES, "reason": "broken", "thought": "t", **kw}


@pytest.fixture
def pending(server, fake_llm):
    """A customer asks for a $348 return, says yes, and is left waiting."""
    fake_llm.script(
        Reply(tool_calls=[tool_call("start_return", **args())]),
        Reply(content="Shall I start the return?"),
        Reply(tool_calls=[tool_call("start_return", **args(confirmed=True))]),
        Reply(content="It is with a reviewer."),
    )
    customer = Client(server)
    customer.chat("Return my headphones 112-1111111-1111111, they're broken")
    code, body = customer.chat("yes")
    assert code == 200
    return customer, body


# --------------------------------------------------------------------------
# The chat contract
# --------------------------------------------------------------------------

def test_chat_reports_the_pending_approval_from_the_database(pending, ran):
    _, body = pending
    (a,) = body["approvals"]

    assert body["request_status"] == "PENDING_APPROVAL"
    assert a["id"] == body["request_id"]
    assert approvals.default().get(a["id"])["status"] == a["status"] == "PENDING_APPROVAL"
    assert a["label"] == "Waiting for staff approval"
    assert (a["action"], a["order_id"]) == ("Return", HEADPHONES)
    assert a["deadline"]
    assert body["escalation"] is None                   # not a support ticket
    assert body["new_actions"] == [] and "start_return" not in ran


def test_chat_shows_the_customer_nothing_internal(pending):
    _, body = pending
    (a,) = body["approvals"]
    assert set(a) == {"id", "status", "label", "action", "order_id", "deadline"}
    text = json.dumps(body["approvals"])
    for secret in ("reason", "broken", "policy", "limit", "reviewer", TOKEN):
        assert secret not in text


def test_an_esc_ticket_is_reported_apart_from_approvals(server, fake_llm):
    fake_llm.script(Reply(tool_calls=[tool_call("escalate", summary="wants a human", thought="t")]),
                    Reply(content="A human will email you."))
    code, body = Client(server).chat("get me a human")

    assert code == 200
    assert body["escalation"] == "ESC-4417"
    assert body["approvals"] == []
    assert body["request_status"] == "COMPLETED"


def test_a_turn_with_nothing_protected_completes_as_before(server, fake_llm, ran):
    fake_llm.script(Reply(tool_calls=[tool_call("get_order", order_id=HEADPHONES, thought="t")]),
                    Reply(content="It was delivered."))
    code, body = Client(server).chat("status of 112-1111111-1111111?")

    assert code == 200 and body["reply"] == "It was delivered."
    assert body["request_status"] == "COMPLETED" and body["approvals"] == []
    assert ran == ["get_order"]


def test_an_under_limit_cancel_still_runs_on_the_customers_yes(server, fake_llm, ran):
    fake_llm.script(
        Reply(tool_calls=[tool_call("cancel_order", order_id=KINDLE, thought="t")]),
        Reply(content="Cancel it?"),
        Reply(tool_calls=[tool_call("cancel_order", order_id=KINDLE, confirmed=True, thought="t")]),
        Reply(content="Cancelled."),
    )
    c = Client(server)
    c.chat("cancel 112-3333333-3333333")
    _, body = c.chat("yes")

    assert body["request_status"] == "COMPLETED" and body["approvals"] == []
    assert ran == ["cancel_order"]
    assert any("Cancelled 112-3333333-3333333" in a for a in body["actions"])


def test_state_after_a_reload_still_shows_the_pending_request(pending):
    customer, body = pending
    code, st = customer.call("GET", "/state")
    assert code == 200 and st["approvals"] == body["approvals"]


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------

def test_a_customer_can_look_up_their_own_request(pending):
    customer, body = pending
    code, r = customer.call("GET", f"/requests/{body['request_id']}")
    assert code == 200 and r == body["approvals"][0]


def test_another_customer_cannot_see_the_request(pending, server):
    _, body = pending
    stranger = Client(server)
    code, r = stranger.call("GET", f"/requests/{body['request_id']}")
    assert code == 404 and body["request_id"] not in json.dumps(r)
    _, st = stranger.call("GET", "/state")
    assert st["approvals"] == []


def test_a_customer_lookup_of_a_malformed_id_looks_like_not_found(server):
    assert Client(server).call("GET", "/requests/not-an-id")[0] == 404


# --------------------------------------------------------------------------
# Reviewer authentication
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [("GET", "/approvals"), ("POST", "/approvals/sweep")])
def test_reviewer_endpoints_refuse_a_missing_token(server, method, path):
    code, body = Client(server).call(method, path)
    assert code == 403 and "requests" not in body


def test_reviewer_endpoints_refuse_a_wrong_token(pending, server, ran):
    _, body = pending
    c = Client(server, token="wrong")
    assert c.call("GET", "/approvals")[0] == 403
    assert c.call("POST", f"/approvals/{body['request_id']}/approve", {"reviewer": "x"})[0] == 403
    assert approvals.default().get(body["request_id"])["status"] == "PENDING_APPROVAL"
    assert "start_return" not in ran


def test_reviewer_endpoints_are_off_without_a_configured_token(server, monkeypatch):
    monkeypatch.delenv("AMI_REVIEWER_TOKEN")
    code, body = Client(server, token="anything").call("GET", "/approvals")
    assert code == 403 and "AMI_REVIEWER_TOKEN" in body["error"]


def test_a_customer_cannot_approve_their_own_request(pending, ran):
    customer, body = pending                            # no reviewer token
    code, _ = customer.call("POST", f"/approvals/{body['request_id']}/approve", {"reviewer": "me"})
    assert code == 403 and "start_return" not in ran


# --------------------------------------------------------------------------
# Reviewer actions
# --------------------------------------------------------------------------

def test_a_reviewer_lists_and_reads_pending_requests(pending, server):
    _, body = pending
    rid = body["request_id"]
    reviewer = Client(server, TOKEN)

    code, listing = reviewer.call("GET", "/approvals")
    assert code == 200 and [r["id"] for r in listing["requests"]] == [rid]

    code, one = reviewer.call("GET", f"/approvals/{rid}")
    assert code == 200 and one["tool"] == "start_return"
    assert [e["to_status"] for e in one["events"]] == ["RECEIVED", "DRAFTED", "PENDING_APPROVAL"]


def test_a_reviewer_approves_and_it_runs_once(pending, server, ran):
    customer, body = pending
    rid = body["request_id"]
    reviewer = Client(server, TOKEN)

    code, r = reviewer.call("POST", f"/approvals/{rid}/approve", {"reviewer": "dana"})
    assert code == 200 and r["status"] == "COMPLETED" and r["outcome"]["rma"]

    code, again = reviewer.call("POST", f"/approvals/{rid}/approve", {"reviewer": "dana"})
    assert code == 409
    assert reviewer.call("POST", f"/approvals/{rid}/resume")[0] == 200     # idempotent
    assert ran.count("start_return") == 1

    _, st = customer.call("GET", "/state")
    assert st["approvals"][0]["label"] == "Approved and done"


def test_a_reviewer_rejects_and_nothing_runs(pending, server, ran):
    customer, body = pending
    rid = body["request_id"]
    reviewer = Client(server, TOKEN)

    code, r = reviewer.call("POST", f"/approvals/{rid}/reject", {"reviewer": "dana", "reason": "used"})
    assert code == 200 and r["decision"] == "rejected" and r["execution_status"] == "not_executed"
    assert reviewer.call("POST", f"/approvals/{rid}/approve", {"reviewer": "sam"})[0] == 409
    assert reviewer.call("POST", f"/approvals/{rid}/resume")[0] == 409
    assert "start_return" not in ran

    _, st = customer.call("GET", "/state")
    assert st["approvals"][0]["label"] == "Not approved, so nothing was changed"
    assert "used" not in json.dumps(st["approvals"])      # the reviewer's reason stays internal


@pytest.mark.parametrize("path,code", [
    ("/approvals/REQ-xyz/approve", 400),
    ("/approvals/REQ-000000000000/approve", 404),
    ("/approvals/REQ-000000000000/explode", 404),
])
def test_bad_ids_and_routes_get_client_errors(server, path, code):
    assert Client(server, TOKEN).call("POST", path, {"reviewer": "dana"})[0] == code


def test_a_decision_without_a_reviewer_name_is_refused(pending, server, ran):
    _, body = pending
    code, _ = Client(server, TOKEN).call("POST", f"/approvals/{body['request_id']}/approve", {})
    assert code == 400 and "start_return" not in ran


def test_an_unknown_status_filter_is_refused(server):
    assert Client(server, TOKEN).call("GET", "/approvals?status=bogus")[0] == 400


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_the_page_renders_approvals_from_server_fields_as_text(web):
    page = web.PAGE
    assert "d.approvals" in page
    for field in ("a.label", "a.id", "a.status", "a.deadline"):
        assert field in page
    block = page[page.index("for (const a of d.approvals"):page.index("function watchApprovals")]
    assert "innerHTML = '<div" in block and "+ a." not in block.split("textContent")[0]
    assert "entry review" in block and "entry esc" not in block
    assert TOKEN not in page and "X-Reviewer-Token" not in page

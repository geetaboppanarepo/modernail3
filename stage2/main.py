"""Amazon support agent — profile, memory, planning, action, policy, RAG.

    python3 main.py                  ReAct planner, with the trace
    python3 main.py --plan           plan-and-execute planner
    python3 main.py --quiet          trace hidden
    python3 main.py --baseline       the original loop: no planning, no memory

Type 'memory' at the prompt to dump what the agent currently holds.

Stage 2: --plan selects the second planner; every turn passes through
policy.check_input and policy.check_output; long-term memory is read
before the turn and written after it.

HITL: each turn is a request in state/approvals.db; a large refund is
left PENDING_APPROVAL for `python3 review.py` to decide, and the decision
is folded in at the start of the next turn.

Model routing: each turn is classified by ami/router.py (cheap or strong
model); with the trace on, the decision and any escalation are printed.
"""

import sys
import uuid

from ami import agent_profile as profile
from ami import approvals
from ami import plan_execute
from ami import planner
from ami import policy
from ami import router
from ami.agent import respond
from ami.memory import ConversationMemory, LongTermMemory, WorkingMemory


def main():
    argv = sys.argv[1:]
    baseline = "--baseline" in argv
    use_plan = "--plan" in argv
    trace = "--quiet" not in argv

    system = profile.system_prompt()
    if use_plan:
        system += plan_execute.PLANNING_RULES
    elif not baseline:
        system += planner.PLANNING_RULES

    convo = ConversationMemory(system)     # what was said
    work = WorkingMemory()                 # what is known and done
    work.session_id = "cli-" + uuid.uuid4().hex[:8]
    longterm = LongTermMemory()            # what we know about this customer
    mode = "baseline" if baseline else ("plan-execute" if use_plan else "ReAct")

    print(f"[{profile.NAME} · {router.label()} · {mode}]  (ctrl-c or 'quit' to exit)\n")
    print(f"{profile.NAME}: {profile.GREETING}\n")

    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user:
            continue
        if user.lower() in {"quit", "exit"}:
            break

        if user.lower() == "memory":
            print(f"\n-- conversation memory: {len(convo)} messages")
            print("-- working memory:")
            print(work.brief() or "   (empty)")
            print("-- long-term memory:")
            print(longterm.recall(work.customer_email, work.session_id)
                  or "   (not a returning customer)")
            print()
            continue

        # policy: input
        text, note = policy.check_input(user, work)
        work.turn += 1
        note = "\n\n".join(filter(None, [note, approvals.sync(work, work.session_id)])) or None
        earlier = [m["content"] for m in convo.history if m.get("role") == "user"][-6:]
        request_id = approvals.begin(text, session_id=work.session_id, context=earlier)
        convo.add_user(text)
        print()

        route = router.begin(text, work, note)
        if trace:
            print(f"  [route] {route.decision['reason']}\n")
        try:
            if baseline:
                reply = respond(convo.messages(), verbose=trace)
            elif use_plan:
                reply = plan_execute.plan_execute(convo, work, trace=trace,
                                                  longterm=longterm, extra=note)
            else:
                reply = planner.react(convo, work, trace=trace,
                                      longterm=longterm, extra=note)
        finally:
            routing = router.end()
        if trace and routing["escalated"]:
            print(f"  [route] escalated to strong: "
                  f"{'; '.join(e['why'] for e in routing['escalations'])}\n")

        # policy: a human where policy says so; output; then remember the customer
        reply = policy.enforce_handoff(reply, work)
        reply = policy.check_output(
            reply, work, text,
            context=longterm.recall(work.customer_email, work.session_id) or "")
        longterm.remember(work, session_id=work.session_id)
        request = approvals.finish(request_id, reply, routing)

        print(f"{profile.NAME}: {reply}\n")
        if request["status"] != approvals.COMPLETED:
            print(f"   [{request_id} is {request['status']} — "
                  f"decide with: python3 review.py list]\n")


if __name__ == "__main__":
    main()

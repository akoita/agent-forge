"""Unit tests for the Super-Agent ticket ledger, repartition router, admission, and inbox."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from agent_forge.agent.memory import RepoMemoryStore
from agent_forge.config import ForgeConfig, load_config
from agent_forge.orchestration import (
    AdmissionPolicy,
    AttentionBudget,
    EventBus,
    EventType,
    InvalidTicketTransitionError,
    ProjectInput,
    RiskLevel,
    SuperAgentRouter,
    Ticket,
    TicketBudget,
    TicketDraft,
    TicketKind,
    TicketLedger,
    TicketState,
    capture_project_context,
    is_scope_glob_safe,
    scopes_overlap,
    serialize_overlapping_scopes,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestTicketStateMachineAndLedger:
    """Ticket lifecycle transitions, durable JSONL persistence, and crash recovery."""

    def test_valid_and_invalid_transitions(self) -> None:
        ledger = TicketLedger()
        ticket = ledger.add_ticket(
            Ticket(
                parent_input_id="inp-1",
                title="Fix health endpoint",
                goal="Return 200 OK",
                scope_globs=["src/health.py"],
            )
        )
        assert ticket.state == TicketState.INTAKE

        with pytest.raises(InvalidTicketTransitionError):
            ledger.transition_ticket(ticket.ticket_id, TicketState.DISPATCHED)

        for next_state in (
            TicketState.TRIAGED,
            TicketState.ADMITTED,
            TicketState.DISPATCHED,
            TicketState.VERIFYING,
            TicketState.READY_FOR_REVIEW,
            TicketState.INTEGRATED,
        ):
            updated = ledger.transition_ticket(
                ticket.ticket_id,
                next_state,
                verification_summary="pytest passed"
                if next_state == TicketState.READY_FOR_REVIEW
                else None,
            )
            assert updated.state == next_state

        with pytest.raises(InvalidTicketTransitionError):
            ledger.transition_ticket(ticket.ticket_id, TicketState.ADMITTED)

    def test_durable_recovery_ignores_truncated_tail_and_preserves_idempotency(
        self, tmp_path: Path
    ) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = TicketLedger(ledger_dir)

        inp = ledger.record_input(
            ProjectInput(raw_text="Add endpoint", repo_path=str(tmp_path)),
            idempotency_key="inp-key-1",
        )
        # Replaying same idempotency key returns original object cleanly
        dup_inp = ledger.record_input(
            ProjectInput(raw_text="Different text", repo_path=str(tmp_path)),
            idempotency_key="inp-key-1",
        )
        assert dup_inp.input_id == inp.input_id

        tkt = ledger.add_ticket(
            Ticket(
                parent_input_id=inp.input_id,
                title="Implement route",
                goal="Add GET /ping",
                scope_globs=["src/api.py"],
            ),
            idempotency_key="tkt-add-1",
        )
        ledger.transition_ticket(tkt.ticket_id, TicketState.TRIAGED, idempotency_key="tkt-tr-1")
        ledger.transition_ticket(tkt.ticket_id, TicketState.ADMITTED, idempotency_key="tkt-adm-1")

        # Simulate mid-write crash by removing snapshot and appending broken JSON line
        (ledger_dir / "state.json").unlink()
        with (ledger_dir / "tickets.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 999, "op": "ticket", "payload": ')

        recovered = TicketLedger.recover(ledger_dir)
        restored_tkt = recovered.get_ticket(tkt.ticket_id)
        assert restored_tkt.state == TicketState.ADMITTED
        assert recovered.has_idempotency_key("inp-key-1")
        assert recovered.has_idempotency_key("tkt-adm-1")
        assert recovered.get_input(inp.input_id).raw_text == "Add endpoint"
        assert json.loads((ledger_dir / "state.json").read_text(encoding="utf-8"))["tickets"]


class TestScopeOverlapAndDAG:
    """Scope glob safety, overlap serialization, and cycle rejection."""

    def test_scope_glob_safety_and_overlap_checks(self) -> None:
        assert is_scope_glob_safe("src/auth/*.py") is True
        assert is_scope_glob_safe("apps/studio/src/App.tsx") is True
        for unsafe in (
            "../secret.py",
            "src/../../etc/passwd",
            "/etc/passwd",
            ".git/hooks/pre-commit",
            "",
        ):
            assert is_scope_glob_safe(unsafe) is False

        assert scopes_overlap(["src/a.py"], ["src/b.py"]) is False
        assert scopes_overlap(["src/a.py"], ["src/a.py"]) is True
        assert scopes_overlap(["src/auth/**"], ["src/auth/login.py"]) is True
        assert scopes_overlap(["**"], ["docs/readme.md"]) is True

    def test_overlap_serialization_and_read_only_parallelism(self) -> None:
        ledger = TicketLedger()
        t_res = Ticket(
            ticket_id="t-research",
            parent_input_id="i1",
            title="Inspect auth",
            goal="Inspect auth module",
            kind=TicketKind.RESEARCH,
            scope_globs=["src/auth/**"],
            state=TicketState.ADMITTED,
        )
        t_a = Ticket(
            ticket_id="t-a",
            parent_input_id="i1",
            title="Edit auth login",
            goal="Update login",
            kind=TicketKind.IMPLEMENT,
            scope_globs=["src/auth/login.py"],
            state=TicketState.ADMITTED,
        )
        t_b = Ticket(
            ticket_id="t-b",
            parent_input_id="i1",
            title="Edit auth tests",
            goal="Test auth module",
            kind=TicketKind.TEST,
            scope_globs=["src/auth/*.py"],
            state=TicketState.ADMITTED,
        )
        t_c = Ticket(
            ticket_id="t-c",
            parent_input_id="i1",
            title="Edit billing",
            goal="Update billing",
            kind=TicketKind.IMPLEMENT,
            scope_globs=["src/billing.py"],
            state=TicketState.ADMITTED,
        )

        ordered = serialize_overlapping_scopes([t_res, t_a, t_b, t_c])
        for item in ordered:
            ledger.add_ticket(item)

        # Read-only t-research never blocks mutating work; overlapping t-b waits for t-a.
        assert t_res.ticket_id not in ledger.get_ticket("t-a").depends_on
        assert "t-a" in ledger.get_ticket("t-b").depends_on
        assert ledger.get_ticket("t-c").depends_on == []
        ready_ids = [t.ticket_id for t in ledger.ready_to_dispatch()]
        assert set(ready_ids) == {"t-research", "t-a", "t-c"}
        assert not ledger.has_cycle()


class TestAdmissionAndAttentionBudget:
    """Explicit authorization matrix and bounded human Inbox coalescing."""

    def test_admission_matrix_rules(self) -> None:
        escape_tkt = Ticket(
            parent_input_id="i1",
            title="Escape",
            goal="Read parent",
            scope_globs=["../secrets.txt"],
        )
        # Fail-closed even in auto mode
        dec = AdmissionPolicy(mode="auto").evaluate(escape_tkt)
        assert dec.admitted is False
        assert dec.category == "boundary_escape"

        ext_tkt = Ticket(
            parent_input_id="i1",
            title="Push release",
            goal="Run git push origin main",
            scope_globs=["src/app.py"],
        )
        dec_ext = AdmissionPolicy(mode="guarded").evaluate(ext_tkt)
        assert dec_ext.admitted is False
        assert dec_ext.category == "external_or_destructive"

        budget_tkt = Ticket(
            parent_input_id="i1",
            title="Huge task",
            goal="Rewrite everything",
            scope_globs=["src/app.py"],
            budget=TicketBudget(max_tokens=100_000),
        )
        dec_budget = AdmissionPolicy(mode="guarded", max_total_tokens=150_000).evaluate(
            budget_tkt,
            allocated_tokens=80_000,
        )
        assert dec_budget.admitted is False
        assert dec_budget.inbox_kind == "budget"

        mut_tkt = Ticket(
            parent_input_id="i1",
            title="Edit file",
            goal="Update config",
            kind=TicketKind.IMPLEMENT,
            risk_level=RiskLevel.LOW,
            scope_globs=["src/config.py"],
        )
        assert AdmissionPolicy(mode="strict").evaluate(mut_tkt).admitted is False
        assert AdmissionPolicy(mode="guarded").evaluate(mut_tkt).admitted is True

        high_risk = mut_tkt.model_copy(update={"risk_level": RiskLevel.HIGH})
        assert AdmissionPolicy(mode="guarded").evaluate(high_risk).admitted is False

    def test_attention_budget_coalesces_and_resolves_blocked_tickets(self) -> None:
        ledger = TicketLedger()
        budget = AttentionBudget(max_active_interrupts=2, max_interrupts_per_window=2)
        tickets = []
        for idx in range(4):
            t = ledger.add_ticket(
                Ticket(
                    ticket_id=f"t-{idx}",
                    parent_input_id="i1",
                    title=f"Action {idx}",
                    goal=f"Goal {idx}",
                    scope_globs=[f"src/mod_{idx}.py"],
                )
            )
            ledger.transition_ticket(t.ticket_id, TicketState.TRIAGED)
            ledger.transition_ticket(
                t.ticket_id, TicketState.BLOCKED_ON_HUMAN, blocked_reason="Need approval"
            )
            tickets.append(t)
            budget.enqueue_escalation(
                ledger,
                kind="approval",
                ticket_ids=[t.ticket_id],
                summary=f"Approve {t.ticket_id}",
            )

        unresolved = ledger.list_inbox(unresolved_only=True)
        assert len(unresolved) == 2  # Strictly capped at max_active_interrupts

        batched = next(it for it in unresolved if len(it.ticket_ids) > 1)
        transitioned = budget.resolve_item(
            ledger, batched.item_id, "approve", resolution_note="LGTM"
        )
        assert all(t.state == TicketState.ADMITTED for t in transitioned)
        for tid in batched.ticket_ids:
            assert ledger.get_ticket(tid).state == TicketState.ADMITTED


class TestSuperAgentRouterEndToEnd:
    """Intake normalization, memory/repo context, repartition, and EventBus updates."""

    @pytest.mark.asyncio
    async def test_router_partitions_disjoint_and_overlapping_tasks_with_events(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "alpha.py").write_text("A = 1\n", encoding="utf-8")
        (tmp_path / "src" / "beta.py").write_text("B = 1\n", encoding="utf-8")
        RepoMemoryStore(tmp_path).add("Always run pytest after edits", category="testing")

        snapshot = capture_project_context(tmp_path)
        assert "src/alpha.py" in snapshot.files
        assert any("Always run pytest" in m.text for m in snapshot.memory_entries)

        bus = EventBus()
        seen_events: list[EventType] = []

        async def _record(ev: Any) -> None:
            seen_events.append(ev.type)

        for etype in (
            EventType.SUPER_INPUT_RECEIVED,
            EventType.SUPER_TICKET_UPDATED,
            EventType.SUPER_INBOX_UPDATED,
        ):
            await bus.subscribe(etype, _record)

        ledger = TicketLedger(tmp_path / "super-ledger")
        router = SuperAgentRouter(
            repo_path=tmp_path,
            ledger=ledger,
            admission=AdmissionPolicy(mode="guarded", max_total_tokens=400_000),
            attention_budget=AttentionBudget(max_active_interrupts=2),
            event_bus=bus,
        )

        prompt = (
            "1. Update validation logic in src/alpha.py\n"
            "2. Refactor helper in src/beta.py\n"
            "3. Add unit tests for src/alpha.py\n"
            "4. Run git push origin feat/all after verification"
        )
        res = await router.route_input(prompt, source="studio", idempotency_key="req-42")
        assert len(res.tickets) == 4
        assert len(res.admitted_ids) == 3
        assert len(res.blocked_ids) == 1
        assert len(res.inbox_items) == 1
        assert "Always run pytest" in res.tickets[0].goal

        # src/alpha.py is edited by ticket 0 and tested by ticket 2 (serialized).
        t0, t1, t2, t3 = res.tickets
        assert t0.ticket_id in t2.depends_on
        assert t1.depends_on == []
        assert t3.state == TicketState.BLOCKED_ON_HUMAN

        ready = {t.ticket_id for t in ledger.ready_to_dispatch()}
        assert ready == {t0.ticket_id, t1.ticket_id}
        assert EventType.SUPER_INPUT_RECEIVED in seen_events
        assert EventType.SUPER_TICKET_UPDATED in seen_events
        assert EventType.SUPER_INBOX_UPDATED in seen_events

        # Idempotent replay returns identical tickets without duplicating state
        replay = await router.route_input(prompt, source="studio", idempotency_key="req-42")
        assert [t.ticket_id for t in replay.tickets] == [t.ticket_id for t in res.tickets]

    @pytest.mark.asyncio
    async def test_router_rejects_cyclic_custom_planner(self, tmp_path: Path) -> None:
        async def _cyclic_planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="A", goal="Task A", depends_on_indices=[1], scope_globs=["a.py"]),
                TicketDraft(title="B", goal="Task B", depends_on_indices=[0], scope_globs=["b.py"]),
            ]

        router = SuperAgentRouter(repo_path=tmp_path, planner_fn=_cyclic_planner)
        with pytest.raises(ValueError, match="dependency cycle"):
            await router.route_input("Run cyclic work")

    def test_super_agent_config_loads_defaults(self, tmp_path: Path) -> None:
        cfg = ForgeConfig()
        assert cfg.super_agent.enabled is False
        assert cfg.super_agent.admission_mode == "guarded"
        assert cfg.super_agent.max_active_interrupts == 3

        repo_cfg = load_config()
        assert repo_cfg.super_agent.max_concurrent_workers == 3
        assert repo_cfg.super_agent.max_total_tokens == 400_000

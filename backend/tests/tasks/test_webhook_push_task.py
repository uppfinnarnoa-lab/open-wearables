"""Tests for webhook → sync-log instrumentation in process_webhook_push.

The new-vs-updated split is derived centrally: a provider only has to return a
WriteCounts as its count, and the task surfaces the split — no per-provider
plumbing. Saves become SUCCESS runs, errors FAILED, no-ops SKIPPED, and
deliveries with no resolvable user (or from self-reporting Garmin) are dropped.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

import app.integrations.celery.tasks.webhook_push_task as task
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus


@pytest.fixture
def user_id() -> str:
    return str(uuid4())


def _capture() -> tuple[list[dict], Any]:
    calls: list[dict] = []

    def fake(uid: Any, provider: str, **kw: Any) -> None:
        calls.append({"provider": provider, "user_id": str(uid), **kw})

    return calls, fake


def test_writecounts_split_surfaces_without_explicit_keys(user_id: str) -> None:
    """A WriteCounts count yields the new/updated split centrally (no records_inserted keys)."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status(
            "oura",
            {
                "status": "processed",
                "data_type": "daily_activity",
                "records_saved": WriteCounts(0, 3),
                "user_id": user_id,
            },
        )
    assert len(calls) == 1
    c = calls[0]
    assert c["status"] == SyncStatus.SUCCESS
    assert c["items_processed"] == 3
    assert c["metadata"] == {"inserted": 0, "updated": 3}
    assert c["message"] == "webhook 0 new, 3 updated"


def test_plain_int_count_has_no_breakdown(user_id: str) -> None:
    """Workout-style providers returning a plain int still log a success run, no split."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("strava", {"status": "processed", "records_saved": 2, "user_id": user_id})
    assert calls[0]["status"] == SyncStatus.SUCCESS
    assert calls[0]["items_processed"] == 2
    assert calls[0]["metadata"] is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "error", "error": "boom"}, SyncStatus.FAILED),
        ({"status": "ignored", "reason": "duplicate_activity"}, SyncStatus.SKIPPED),
        ({"status": "processed", "records_saved": 0}, SyncStatus.SKIPPED),  # saved nothing
    ],
)
def test_status_mapping(result: dict, expected: SyncStatus, user_id: str) -> None:
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("oura", {**result, "user_id": user_id})
    assert calls[0]["status"] == expected


def test_no_user_resolved_is_dropped() -> None:
    """user_not_found / invalid payloads carry no user_id → nothing to attribute."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("oura", {"status": "user_not_found", "oura_user_id": "x"})
    assert calls == []


def test_self_reporting_provider_excluded(user_id: str) -> None:
    """Garmin reports its own sync status; the shared task must not duplicate it."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("garmin", {"status": "processed", "saved_count": 9, "user_id": user_id})
    assert calls == []


def test_emission_never_raises() -> None:
    """Sync-log emission must never break webhook processing."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("oura", None)
        task._emit_webhook_sync_status("oura", {"status": "processed", "records_saved": 1, "user_id": "not-a-uuid"})
    assert calls == []


class TestUnattributedDelivery:
    """A delivery that resolves to no user is data we accepted and threw away.

    There is genuinely no user to attribute a sync run to, so no sync-status
    event can be emitted -- that part of the old behaviour is correct and stays.
    What was wrong is that the delivery then vanished: a warning, and a Celery
    task reporting 'succeeded'.

    Suunto pushed 77 events for a real person over six days while his OAuth
    connection silently did not exist (a wrong client secret, fixed 2026-09-05).
    Every one was accepted, enqueued, dropped and reported as success. Nothing in
    the sync log, no counter, no alertable line -- the only trace was 77 identical
    warnings nobody was reading. Six days of someone's health data, discarded in
    a way the system called normal.
    """

    def test_still_emits_no_sync_event(self) -> None:
        """No user means no run to attribute -- that much was always right."""
        calls, fake = _capture()
        with patch.object(task.sync_status_service, "webhook_delivered", side_effect=fake):
            task._emit_webhook_sync_status("suunto", {"status": "user_not_found"}, "nils26071")
        assert calls == []

    def test_logs_at_error_with_a_stable_action(self) -> None:
        """Loud and greppable, so a rising count is something you can alert on."""
        with (
            patch.object(task.sync_status_service, "webhook_delivered"),
            patch.object(task, "log_structured") as logged,
        ):
            task._emit_webhook_sync_status("suunto", {"status": "user_not_found"}, "nils26071")

        assert logged.call_args_list, "an unattributed delivery must be logged"
        level = logged.call_args_list[-1].args[1]
        fields = logged.call_args_list[-1].kwargs
        assert level == "error"
        assert fields.get("action") == "webhook_delivery_unattributed"
        assert fields.get("provider_user_id") == "nils26071"

    def test_counts_repeat_offenders(self) -> None:
        """The 77th drop must not look like the 1st."""
        identity = f"user-{uuid4()}"
        counts = []
        with (
            patch.object(task.sync_status_service, "webhook_delivered"),
            patch.object(task, "log_structured") as logged,
        ):
            for _ in range(3):
                task._emit_webhook_sync_status("suunto", {"status": "user_not_found"}, identity)
                counts.append(logged.call_args_list[-1].kwargs.get("dropped_total"))
        assert counts == [1, 2, 3]

    def test_never_raises_when_the_counter_is_unavailable(self) -> None:
        """Bookkeeping must not break webhook processing -- it is still only a log."""
        with (
            patch.object(task.sync_status_service, "webhook_delivered"),
            patch.object(task, "get_redis_client", side_effect=RuntimeError("redis down")),
            patch.object(task, "log_structured") as logged,
        ):
            task._emit_webhook_sync_status("suunto", {"status": "user_not_found"}, "nils26071")
        assert logged.call_args_list, "the line must still be written without a counter"

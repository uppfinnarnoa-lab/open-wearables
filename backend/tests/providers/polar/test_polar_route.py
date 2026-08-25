"""
Tests for Polar exercise route ingestion.

Polar's exercise route is the only place a Polar workout's GPS track exists.
It rides along in the same ``/v3/exercises`` response, but only when the
``route`` flag is asked for -- and the sync task names none of Polar's flags,
so before this it was never requested and a Polar workout could not be drawn
on a map.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.schemas.enums import SeriesType
from app.schemas.providers.polar import RoutePointJSON
from app.services.providers.polar.workouts import PolarWorkouts


@pytest.fixture
def workouts() -> PolarWorkouts:
    from app.models import EventRecord, User
    from app.repositories.event_record_repository import EventRecordRepository
    from app.repositories.user_connection_repository import UserConnectionRepository
    from app.repositories.user_repository import UserRepository
    from app.services.providers.polar.oauth import PolarOAuth

    connection_repo = UserConnectionRepository()
    oauth = PolarOAuth(
        user_repo=UserRepository(User),
        connection_repo=connection_repo,
        provider_name="polar",
        api_base_url="https://www.polaraccesslink.com",
    )
    return PolarWorkouts(
        workout_repo=EventRecordRepository(EventRecord),
        connection_repo=connection_repo,
        provider_name="polar",
        api_base_url="https://www.polaraccesslink.com",
        oauth=oauth,
    )


START = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)
USER_ID = UUID("11111111-2222-3333-4444-555555555555")


class TestRouteSamples:
    """Tests for turning Polar route points into latitude/longitude samples."""

    def test_each_point_yields_a_latitude_and_a_longitude(self, workouts: PolarWorkouts) -> None:
        route = [
            RoutePointJSON(latitude=59.3, longitude=18.07, time="2026-08-25T10:00:00"),
            RoutePointJSON(latitude=59.4, longitude=18.08, time="2026-08-25T10:00:01"),
        ]

        samples = workouts._route_samples(route, USER_ID, START, "+02:00")

        assert len(samples) == 4
        assert [s.series_type for s in samples] == [
            SeriesType.latitude,
            SeriesType.longitude,
            SeriesType.latitude,
            SeriesType.longitude,
        ]
        assert float(samples[0].value) == pytest.approx(59.3)
        assert float(samples[1].value) == pytest.approx(18.07)
        assert all(s.zone_offset == "+02:00" for s in samples)

    def test_iso_time_is_used_verbatim(self, workouts: PolarWorkouts) -> None:
        route = [RoutePointJSON(latitude=59.3, longitude=18.07, time="2026-08-25T10:05:30")]

        samples = workouts._route_samples(route, USER_ID, START, None)

        assert samples[0].recorded_at == datetime(2026, 8, 25, 10, 5, 30)

    def test_numeric_time_is_read_as_seconds_from_the_start(self, workouts: PolarWorkouts) -> None:
        """Polar has been seen emitting an offset rather than a timestamp.

        Reading it as a timestamp would put the whole track at the epoch, which
        is why the parser falls back instead of assuming one shape.
        """
        route = [RoutePointJSON(latitude=59.3, longitude=18.07, time="90")]

        samples = workouts._route_samples(route, USER_ID, START, None)

        assert samples[0].recorded_at == datetime(2026, 8, 25, 10, 1, 30, tzinfo=timezone.utc)

    def test_missing_time_falls_back_to_the_sample_index(self, workouts: PolarWorkouts) -> None:
        """Distinct timestamps, not one repeated start.

        Samples are keyed on their timestamp, so a track stamped with a single
        value upserts itself down to one point.
        """
        route = [
            RoutePointJSON(latitude=59.3, longitude=18.07),
            RoutePointJSON(latitude=59.4, longitude=18.08),
        ]

        samples = workouts._route_samples(route, USER_ID, START, None)

        assert samples[0].recorded_at == START
        assert samples[2].recorded_at == START + timedelta(seconds=1)

    def test_points_without_coordinates_are_skipped(self, workouts: PolarWorkouts) -> None:
        """A GPS fix can be absent mid-track; the surrounding points still count."""
        route = [
            RoutePointJSON(latitude=59.3, longitude=18.07, time="0"),
            RoutePointJSON(latitude=None, longitude=None, time="1"),
            RoutePointJSON(latitude=59.4, longitude=18.08, time="2"),
        ]

        samples = workouts._route_samples(route, USER_ID, START, None)

        assert len(samples) == 4

    def test_empty_route_yields_nothing(self, workouts: PolarWorkouts) -> None:
        assert workouts._route_samples([], USER_ID, START, None) == []


class TestRouteFlagDefaulting:
    """The sync task passes only start_date/end_date, so the flags must default."""

    def test_route_is_requested_when_sample_ingestion_is_on(self, workouts: PolarWorkouts) -> None:
        with patch("app.services.providers.polar.workouts.settings") as mock_settings:
            mock_settings.ingest_workout_samples = True
            resolved = workouts._sample_flags({"start_date": "2026-06-01T00:00:00Z", "end_date": None})

        assert resolved["route"] is True
        # The caller's own params survive untouched.
        assert resolved["start_date"] == "2026-06-01T00:00:00Z"

    def test_route_is_not_requested_when_sample_ingestion_is_off(self, workouts: PolarWorkouts) -> None:
        with patch("app.services.providers.polar.workouts.settings") as mock_settings:
            mock_settings.ingest_workout_samples = False
            resolved = workouts._sample_flags({"start_date": None, "end_date": None})

        assert resolved["route"] is False

    def test_an_explicit_flag_wins_over_the_setting(self, workouts: PolarWorkouts) -> None:
        """/sync?route=true must still work with sample ingestion switched off."""
        with patch("app.services.providers.polar.workouts.settings") as mock_settings:
            mock_settings.ingest_workout_samples = False
            resolved = workouts._sample_flags({"route": True})

        assert resolved["route"] is True

    def test_the_flag_reaches_the_api_call(self, workouts: PolarWorkouts) -> None:
        with patch.object(workouts, "_make_api_request", return_value=[]) as mock_request:
            workouts.get_workouts_from_api(MagicMock(), USER_ID, route=True)

        assert mock_request.call_args.kwargs["params"]["route"] == "true"


class TestSaveRouteIsNonFatal:
    """A broken track must not cost us the workout row it belongs to."""

    def test_a_failing_bulk_create_is_swallowed_and_logged(self, workouts: PolarWorkouts) -> None:
        raw = MagicMock()
        raw.route = [RoutePointJSON(latitude=59.3, longitude=18.07, time="0")]
        raw.id = "exercise-1"
        record = MagicMock()
        record.start_datetime = START
        record.zone_offset = None

        with patch.object(workouts.data_point_repo, "bulk_create", side_effect=RuntimeError("db down")):
            written = workouts._save_route(MagicMock(), USER_ID, raw, record)

        assert written == 0

    def test_no_route_writes_nothing(self, workouts: PolarWorkouts) -> None:
        raw = MagicMock()
        raw.route = None

        with patch.object(workouts.data_point_repo, "bulk_create") as mock_bulk:
            written = workouts._save_route(MagicMock(), USER_ID, raw, MagicMock())

        assert written == 0
        mock_bulk.assert_not_called()

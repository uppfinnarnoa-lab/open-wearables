"""Tests for the Suunto FIT export path.

The Suunto workout JSON carries summaries only — the FIT export is the sole
source of per-sample data, GPS track above all. These tests run against a real
Suunto FIT file rather than a synthetic one, because the risk being guarded
against is precisely that Suunto's FIT dialect differs from Garmin's.

Fixture: ``tests/fixtures/suunto_9baro_running.fit`` — an interval run recorded
on a Suunto 9 Baro in Finland (2018-10-16), from the ``FITexamples.zip`` that
Suunto publishes for Cloud API partners.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models import EventRecord
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import SeriesType
from app.services.fit_parser import FitParseResult, parse_fit_file
from app.services.providers.suunto.coverage import TIMESERIES
from app.services.providers.suunto.oauth import SuuntoOAuth
from app.services.providers.suunto.workouts import FIT_EXPORT_ENDPOINT, SuuntoWorkouts

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "suunto_9baro_running.fit"
USER_ID = uuid4()
WORKOUT_KEY = "5bd6eb3252ce7b074fc4fa82"


@pytest.fixture(scope="module")
def fit_bytes() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def parsed(fit_bytes: bytes) -> FitParseResult:
    return parse_fit_file(fit_bytes, USER_ID, source="suunto")


@pytest.fixture
def suunto_workouts() -> SuuntoWorkouts:
    connection_repo = UserConnectionRepository()
    oauth = SuuntoOAuth(
        user_repo=MagicMock(),
        connection_repo=connection_repo,
        provider_name="suunto",
        api_base_url="https://cloudapi.suunto.com",
    )
    return SuuntoWorkouts(
        workout_repo=EventRecordRepository(EventRecord),
        connection_repo=connection_repo,
        provider_name="suunto",
        api_base_url="https://cloudapi.suunto.com",
        oauth=oauth,
    )


class TestParseRealSuuntoFit:
    """The generic ANT+ parser must handle a Suunto-written FIT unchanged."""

    def test_produces_samples(self, parsed: FitParseResult) -> None:
        assert len(parsed.samples) > 0

    def test_carries_gps_track(self, parsed: FitParseResult) -> None:
        types = {s.series_type for s in parsed.samples}
        assert SeriesType.latitude in types
        assert SeriesType.longitude in types
        assert SeriesType.elevation in types

    def test_carries_other_expected_series(self, parsed: FitParseResult) -> None:
        types = {s.series_type for s in parsed.samples}
        assert SeriesType.heart_rate in types
        assert SeriesType.speed in types
        assert SeriesType.cadence in types
        assert SeriesType.air_temperature in types

    def test_semicircles_converted_to_degrees(self, parsed: FitParseResult) -> None:
        """The run was recorded in Finland; degrees, not raw semicircles."""
        lat = [float(s.value) for s in parsed.samples if s.series_type == SeriesType.latitude]
        lon = [float(s.value) for s in parsed.samples if s.series_type == SeriesType.longitude]
        assert lat
        assert lon
        assert all(-90.0 <= v <= 90.0 for v in lat)
        assert all(-180.0 <= v <= 180.0 for v in lon)
        assert 60.0 < lat[0] < 61.0
        assert 24.0 < lon[0] < 26.0

    def test_track_points_pair_up(self, parsed: FitParseResult) -> None:
        """A GPX needs lat and lon at the same instant, so the sets must match."""
        lat = {s.recorded_at for s in parsed.samples if s.series_type == SeriesType.latitude}
        lon = {s.recorded_at for s in parsed.samples if s.series_type == SeriesType.longitude}
        assert lat == lon

    def test_laps_become_segments(self, parsed: FitParseResult) -> None:
        """The fixture is an interval session, so it must yield segments."""
        assert len(parsed.segments) > 0


class TestCoverage:
    def test_matrix_advertises_the_track(self) -> None:
        assert SeriesType.latitude in TIMESERIES
        assert SeriesType.longitude in TIMESERIES
        assert SeriesType.elevation in TIMESERIES

    def test_matrix_does_not_overclaim(self) -> None:
        """Suunto FITs carry no running dynamics — the matrix must not say they do."""
        assert SeriesType.running_vertical_oscillation not in TIMESERIES
        assert SeriesType.running_ground_contact_time not in TIMESERIES
        assert SeriesType.running_stride_length not in TIMESERIES


class TestFetchWorkoutFit:
    def test_calls_the_documented_endpoint(self, suunto_workouts: SuuntoWorkouts) -> None:
        """API Zone: suunto-workout-api (path v3/workouts), op export-workout-fit."""
        assert FIT_EXPORT_ENDPOINT == "/v3/workouts/{workout_key}/fit"

        with patch("app.services.providers.suunto.workouts.download_binary_content") as mock_dl:
            mock_dl.return_value = b"FIT"
            result = suunto_workouts.fetch_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY)

        assert result == b"FIT"
        kwargs = mock_dl.call_args.kwargs
        assert kwargs["url"] == f"https://cloudapi.suunto.com/v3/workouts/{WORKOUT_KEY}/fit"
        assert kwargs["provider_name"] == "suunto"

    def test_sends_subscription_key(self, suunto_workouts: SuuntoWorkouts) -> None:
        """Suunto rejects the call without Ocp-Apim-Subscription-Key."""
        with (
            patch.object(suunto_workouts, "_get_suunto_headers", return_value={"Ocp-Apim-Subscription-Key": "abc"}),
            patch("app.services.providers.suunto.workouts.download_binary_content") as mock_dl,
        ):
            mock_dl.return_value = b"FIT"
            suunto_workouts.fetch_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY)

        assert mock_dl.call_args.kwargs["headers"] == {"Ocp-Apim-Subscription-Key": "abc"}


class TestImportWorkoutFit:
    def test_download_failure_is_swallowed(self, suunto_workouts: SuuntoWorkouts) -> None:
        """A workout whose FIT never arrives is still worth keeping."""
        with patch.object(suunto_workouts, "fetch_workout_fit", side_effect=RuntimeError("429")):
            assert suunto_workouts.import_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY) == 0

    def test_unparseable_fit_is_swallowed(self, suunto_workouts: SuuntoWorkouts) -> None:
        with (
            patch.object(suunto_workouts, "fetch_workout_fit", return_value=b"not a fit file"),
            patch("app.services.providers.suunto.workouts.store_fit_file"),
        ):
            assert suunto_workouts.import_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY) == 0

    def test_persists_samples_when_ingestion_enabled(self, suunto_workouts: SuuntoWorkouts, fit_bytes: bytes) -> None:
        with (
            patch.object(suunto_workouts, "fetch_workout_fit", return_value=fit_bytes),
            patch("app.services.providers.suunto.workouts.store_fit_file"),
            patch("app.services.providers.suunto.workouts.settings") as mock_settings,
            patch.object(suunto_workouts, "_save_fit_workout_fields"),
            patch.object(suunto_workouts.data_point_repo, "bulk_create", return_value=4242) as mock_bulk,
        ):
            mock_settings.ingest_workout_samples = True
            written = suunto_workouts.import_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY)

        assert written == 4242
        samples = mock_bulk.call_args.args[1]
        assert any(s.series_type == SeriesType.latitude for s in samples)
        assert all(s.source == "suunto" for s in samples)

    def test_skips_samples_when_ingestion_disabled(self, suunto_workouts: SuuntoWorkouts, fit_bytes: bytes) -> None:
        with (
            patch.object(suunto_workouts, "fetch_workout_fit", return_value=fit_bytes),
            patch("app.services.providers.suunto.workouts.store_fit_file"),
            patch("app.services.providers.suunto.workouts.settings") as mock_settings,
            patch.object(suunto_workouts, "_save_fit_workout_fields"),
            patch.object(suunto_workouts.data_point_repo, "bulk_create") as mock_bulk,
        ):
            mock_settings.ingest_workout_samples = False
            written = suunto_workouts.import_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY)

        assert written == 0
        mock_bulk.assert_not_called()

    def test_archives_the_raw_fit(self, suunto_workouts: SuuntoWorkouts, fit_bytes: bytes) -> None:
        with (
            patch.object(suunto_workouts, "fetch_workout_fit", return_value=fit_bytes),
            patch("app.services.providers.suunto.workouts.store_fit_file") as mock_store,
            patch("app.services.providers.suunto.workouts.settings") as mock_settings,
            patch.object(suunto_workouts, "_save_fit_workout_fields"),
        ):
            mock_settings.ingest_workout_samples = False
            suunto_workouts.import_workout_fit(MagicMock(), USER_ID, WORKOUT_KEY)

        assert mock_store.call_args.kwargs["provider"] == "suunto"
        assert mock_store.call_args.kwargs["activity_id"] == WORKOUT_KEY

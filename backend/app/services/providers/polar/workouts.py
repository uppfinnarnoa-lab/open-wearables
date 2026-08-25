from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID, uuid4

import isodate

from app.config import settings
from app.constants.workout_types.polar import get_unified_workout_type
from app.database import DbSession
from app.models import DataPointSeries
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.schemas.providers.polar import RoutePointJSON
from app.services.event_record_service import event_record_service
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.utils.dates import offset_to_iso
from app.utils.structured_logging import log_structured


class PolarWorkouts(BaseWorkoutsTemplate):
    """Polar implementation of workouts template."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.data_point_repo = DataPointSeriesRepository(DataPointSeries)

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get exercises from Polar API."""
        return self._make_api_request(db, user_id, "/v3/exercises")

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get exercises from Polar API with options."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)

        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, "/v3/exercises", params=params)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed exercise data from Polar API."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)
        return self.get_exercise_detail(db, user_id, workout_id, samples, zones, route)

    def _extract_dates(self, start_timestamp: Any, end_timestamp: Any) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps.

        Note: Polar uses a different format with offset, so this delegates to _extract_dates_with_offset.
        This is required by the base template but not used directly.
        """
        raise NotImplementedError("Use _extract_dates_with_offset for Polar workouts")

    def _extract_dates_with_offset(
        self,
        start_time: str,
        start_time_utc_offset: int,
        duration: str,
    ) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps with UTC offset."""
        start_date = isodate.parse_datetime(start_time)
        offset = timedelta(minutes=start_time_utc_offset)
        start_date = start_date + offset
        duration_td = isodate.parse_duration(duration)
        end_date = start_date + duration_td
        return start_date, end_date

    def _build_metrics(self, raw_workout: PolarExerciseJSON) -> EventRecordMetrics:
        hr_avg = (
            Decimal(str(raw_workout.heart_rate.average))
            if raw_workout.heart_rate and raw_workout.heart_rate.average is not None
            else None
        )
        hr_max = (
            Decimal(str(raw_workout.heart_rate.maximum))
            if raw_workout.heart_rate and raw_workout.heart_rate.maximum is not None
            else None
        )

        energy_burned = Decimal(str(raw_workout.calories)) if raw_workout.calories is not None else None

        distance = Decimal(str(raw_workout.distance)) if raw_workout.distance is not None else None

        return {
            "heart_rate_max": int(hr_max) if hr_max is not None else None,
            "heart_rate_avg": hr_avg,
            "energy_burned": energy_burned,
            "distance": distance,
        }

    def _normalize_workout(
        self,
        raw_workout: PolarExerciseJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Polar exercise to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.sport, raw_workout.detailed_sport_info)

        start_date, end_date = self._extract_dates_with_offset(
            raw_workout.start_time,
            raw_workout.start_time_utc_offset,
            raw_workout.duration,
        )
        duration_seconds = int((end_date - start_date).total_seconds())

        metrics = self._build_metrics(raw_workout)

        # convert from offset minutes to seconds first
        zone_offset = offset_to_iso(raw_workout.start_time_utc_offset * 60)

        record = EventRecordCreate(
            category="workout",
            type=workout_type.value,
            source_name=raw_workout.device,
            device_model=raw_workout.device,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=raw_workout.id,
            source="polar",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    def _build_bundles(
        self,
        raw: list[PolarExerciseJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Polar exercises."""
        for raw_workout in raw:
            yield self._normalize_workout(raw_workout, user_id)

    def _route_samples(
        self,
        route: list[RoutePointJSON],
        user_id: UUID,
        start_datetime: datetime,
        zone_offset: str | None,
    ) -> list[TimeSeriesSampleCreate]:
        """Turn Polar's route array into latitude/longitude samples.

        Polar's route points carry no elevation, so a Polar track is 2D by
        construction -- unlike the FIT-derived tracks from Garmin and Suunto.

        ``time`` is accepted in both shapes seen in the wild: an ISO timestamp,
        and a plain number of seconds from the start of the exercise. Guessing
        one and being wrong would place every point of the track at the epoch,
        so the parser tries the timestamp first and falls back to the offset.

        The last resort is the point's own index, not the exercise start: samples
        are keyed on their timestamp, so stamping a whole track with one value
        would upsert it down to a single point.
        """
        samples: list[TimeSeriesSampleCreate] = []

        for index, point in enumerate(route):
            if point.latitude is None or point.longitude is None:
                continue

            recorded_at = start_datetime + timedelta(seconds=index)
            if point.time is not None:
                try:
                    recorded_at = datetime.fromisoformat(str(point.time).replace("Z", "+00:00"))
                except ValueError:
                    try:
                        recorded_at = start_datetime + timedelta(seconds=float(point.time))
                    except (TypeError, ValueError):
                        pass

            for series_type, value in (
                (SeriesType.latitude, point.latitude),
                (SeriesType.longitude, point.longitude),
            ):
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        zone_offset=zone_offset,
                        value=Decimal(str(value)),
                        series_type=series_type,
                    )
                )

        return samples

    def _save_route(
        self,
        db: DbSession,
        user_id: UUID,
        raw_workout: PolarExerciseJSON,
        record: EventRecordCreate,
    ) -> int:
        """Persist one exercise's GPS track.

        Never raises: a track that is missing or malformed must not cost us the
        workout row it belongs to.
        """
        if not raw_workout.route:
            return 0
        try:
            samples = self._route_samples(raw_workout.route, user_id, record.start_datetime, record.zone_offset)
            written = self.data_point_repo.bulk_create(db, samples)
            return int(written)
        except Exception as e:
            log_structured(
                self.logger,
                "warning",
                "Failed to save Polar route",
                provider=self.provider_name,
                task="save_route",
                user_id=str(user_id),
                exercise_id=raw_workout.id,
                error=str(e),
            )
            return 0

    def _sample_flags(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Default the per-sample flags from the deployment's ingest setting.

        The sync task calls ``load_data(start_date=..., end_date=...)`` and names
        none of Polar's flags, so they all defaulted to False and every exercise
        arrived without its route -- an orienteering track that cannot be drawn.
        The route rides along in the same response, so switching it on costs no
        extra API call, but the per-second rows are exactly the storage that
        ``ingest_workout_samples`` exists to gate, so the flag decides.
        """
        resolved = dict(kwargs)
        resolved.setdefault("route", settings.ingest_workout_samples)
        return resolved

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """Load data from Polar API."""
        kwargs = self._sample_flags(kwargs)
        workouts_data = self.get_workouts_from_api(db, user_id, **kwargs)
        workouts = [PolarExerciseJSON(**w) for w in workouts_data]

        count = 0
        for raw_workout, (record, detail) in zip(workouts, self._build_bundles(workouts, user_id)):
            created_record = event_record_service.create(db, record)
            detail_for_record = detail.model_copy(update={"record_id": created_record.id})
            event_record_service.create_detail(db, detail_for_record)
            self._save_route(db, user_id, raw_workout, record)
            count += 1

        return count

    def fetch_and_save_exercise(self, db: DbSession, user_id: UUID, path: str) -> int:
        """Fetch a single exercise by URL path and save it. Used by webhook handler."""
        params = {"samples": "false", "zones": "false", "route": str(settings.ingest_workout_samples).lower()}
        raw = self._make_api_request(db, user_id, path, params=params)
        if not raw:
            return 0
        exercise = PolarExerciseJSON(**raw)
        count = 0
        for record, detail in self._build_bundles([exercise], user_id):
            created = event_record_service.create(db, record)
            event_record_service.create_detail(db, detail.model_copy(update={"record_id": created.id}))
            self._save_route(db, user_id, exercise, record)
            count += 1
        return count

    def get_exercise_detail(
        self,
        db: DbSession,
        user_id: UUID,
        exercise_id: str,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
    ) -> dict:
        """Get detailed exercise data from Polar API."""
        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, f"/v3/exercises/{exercise_id}", params=params)

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID, uuid4

from app.config import settings
from app.constants.workout_types.suunto import get_unified_workout_type
from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecordDetail
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.event_record_detail_repository import EventRecordDetailRepository
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
)
from app.schemas.providers.suunto import WorkoutJSON as SuuntoWorkoutJSON
from app.services.event_record_service import event_record_service
from app.services.fit_parser import parse_fit_file
from app.services.providers.api_client import download_binary_content
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.services.raw_payload_storage import store_fit_file
from app.utils.dates import offset_to_iso
from app.utils.structured_logging import log_structured

# Suunto Workout API (base path v3/workouts), operation `export-workout-fit`:
#   GET https://cloudapi.suunto.com/v3/workouts/{workoutKey}/fit -> application/octet-stream
# The older /v2/workout/exportFit/{key} form belongs to the API Zone's
# "SUUNTO WORKOUT API (DEPRECATED)" product and must not be used.
FIT_EXPORT_ENDPOINT: str = "/v3/workouts/{workout_key}/fit"


class SuuntoWorkouts(BaseWorkoutsTemplate):
    """Suunto implementation of workouts template."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.data_source_repo = DataSourceRepository(DataSource)
        self.data_point_repo = DataPointSeriesRepository(DataPointSeries)
        self.event_record_detail_repo = EventRecordDetailRepository(EventRecordDetail)

    def _get_suunto_headers(self) -> dict[str, str]:
        """Get Suunto-specific headers including subscription key."""
        headers = {}
        if self.oauth and hasattr(self.oauth, "credentials"):
            subscription_key = self.oauth.credentials.subscription_key
            if subscription_key:
                headers["Ocp-Apim-Subscription-Key"] = subscription_key
        return headers

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get workouts from Suunto API."""
        # Suunto uses 'since' parameter in epoch milliseconds
        since = int(start_date.timestamp() * 1000)
        params = {
            "since": since,
            "limit": 100,
        }
        headers = self._get_suunto_headers()
        response = self._make_api_request(db, user_id, "/v3/workouts/", params=params, headers=headers)
        return response.get("payload", [])

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get workouts from Suunto API with specific options."""
        since = kwargs.get("since", 0)
        limit = kwargs.get("limit", 50)
        offset = kwargs.get("offset", 0)
        filter_by_modification_time = kwargs.get("filter_by_modification_time", True)

        params = {
            "since": since,
            "limit": min(limit, 100),
            "offset": offset,
            "filter-by-modification-time": str(filter_by_modification_time).lower(),
        }

        # Suunto requires subscription key header
        headers = self._get_suunto_headers()

        return self._make_api_request(db, user_id, "/v3/workouts/", params=params, headers=headers)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed workout data from Suunto API."""
        return self.get_workout_detail(db, user_id, workout_id)

    def _extract_dates(self, start_timestamp: int, end_timestamp: int) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps."""
        start_date = datetime.fromtimestamp(start_timestamp / 1000)
        end_date = datetime.fromtimestamp(end_timestamp / 1000)
        return start_date, end_date

    def _build_metrics(self, raw_workout: SuuntoWorkoutJSON) -> EventRecordMetrics:
        """Build metrics from Suunto workout data.

        Note: For heart rate, use hrmax/workoutMaxHR (actual max during workout),
        NOT max (which is userMaxHR from settings).
        """
        hr_data = raw_workout.hrdata

        # Heart rate - use hrmax (actual workout max), not max (user's max HR from settings)
        heart_rate_avg = None
        heart_rate_max = None
        heart_rate_min = None

        if hr_data:
            # Average HR
            if hr_data.avg is not None:
                heart_rate_avg = Decimal(str(hr_data.avg))
            elif hr_data.workoutAvgHR is not None:
                heart_rate_avg = Decimal(str(hr_data.workoutAvgHR))

            # Max HR - use hrmax (actual workout max), fallback to workoutMaxHR
            if hr_data.hrmax is not None:
                heart_rate_max = Decimal(str(hr_data.hrmax))
            elif hr_data.workoutMaxHR is not None:
                heart_rate_max = Decimal(str(hr_data.workoutMaxHR))

            # Min HR
            if hr_data.min is not None:
                heart_rate_min = int(hr_data.min)

        # Steps
        steps_count = int(raw_workout.stepCount) if raw_workout.stepCount is not None else None

        energy_burned = (
            Decimal(str(raw_workout.energyConsumption)) if raw_workout.energyConsumption is not None else None
        )

        distance = Decimal(str(raw_workout.totalDistance)) if raw_workout.totalDistance is not None else None

        return {
            "heart_rate_min": heart_rate_min,
            "heart_rate_max": int(heart_rate_max) if heart_rate_max is not None else None,
            "heart_rate_avg": heart_rate_avg,
            "steps_count": steps_count,
            # Energy and distance
            "energy_burned": energy_burned,
            "distance": distance,
            # Speed (convert from m/s to km/h for display)
            "max_speed": Decimal(str(raw_workout.maxSpeed * 3.6)) if raw_workout.maxSpeed else None,
            "average_speed": Decimal(str(raw_workout.avgSpeed * 3.6)) if raw_workout.avgSpeed else None,
            # Power
            "max_watts": Decimal(str(raw_workout.maxPower)) if raw_workout.maxPower else None,
            "average_watts": Decimal(str(raw_workout.avgPower)) if raw_workout.avgPower else None,
            # Elevation
            "total_elevation_gain": Decimal(str(raw_workout.totalAscent)) if raw_workout.totalAscent else None,
            "elev_high": Decimal(str(raw_workout.maxAltitude)) if raw_workout.maxAltitude else None,
            "elev_low": Decimal(str(raw_workout.minAltitude)) if raw_workout.minAltitude else None,
        }

    def _normalize_workout(
        self,
        raw_workout: SuuntoWorkoutJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Suunto workout to EventRecordCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.activityId)

        # Fresh webhook payloads omit stopTime, so derive it from startTime + active time + pauses.
        # Suunto's totalTime is the active timer time (FIT total_timer_time), pauses excluded.
        # PauseMarkerExtension carries the gaps; sum them so end_datetime reflects real elapsed time.
        active_time_ms = int(raw_workout.totalTime * 1000)
        pause_total_ms = sum(p.duration_ms for p in raw_workout.pause_markers)
        stop_time_ms = raw_workout.stopTime or raw_workout.startTime + active_time_ms + pause_total_ms
        start_date, end_date = self._extract_dates(raw_workout.startTime, stop_time_ms)
        duration_seconds = int(raw_workout.totalTime)

        zone_offset = None
        if raw_workout.timeOffsetInMinutes is not None:
            zone_offset = offset_to_iso(raw_workout.timeOffsetInMinutes * 60)

        # Newer Suunto watches (e.g. Race 2) deliver gear inside SummaryExtension instead of
        # at the workout root; fall back to that if the top-level field is missing.
        gear = raw_workout.gear or raw_workout.gear_from_summary_extension
        if gear:
            source_name = gear.displayName or gear.name or "Suunto"
            device_model = gear.displayName or gear.name
        else:
            source_name = "Suunto"
            device_model = None

        metrics = self._build_metrics(raw_workout)

        # Moving time (for now same as total time, Suunto may provide this separately)
        moving_time = duration_seconds

        workout_create = EventRecordCreate(
            category="workout",
            type=workout_type.value,
            source_name=source_name,
            device_model=device_model,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=str(raw_workout.workoutId),
            source=self.provider_name,  # Provider name for mapping (e.g., "suunto")
            user_id=user_id,
        )

        # Add moving_time to metrics for workout_detail
        metrics["moving_time_seconds"] = moving_time

        workout_detail_create = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return workout_create, workout_detail_create

    def _build_bundles(
        self,
        raw: list[SuuntoWorkoutJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Suunto workouts."""
        for raw_workout in raw:
            record, details = self._normalize_workout(raw_workout, user_id)
            yield record, details

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """Load data from Suunto API."""
        # Handle generic start_date/end_date
        start_date = kwargs.get("start_date")

        api_kwargs = kwargs.copy()

        # Convert start_date to 'since' timestamp (Suunto expects epoch milliseconds)
        if start_date:
            if isinstance(start_date, str):
                try:
                    start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                    api_kwargs["since"] = int(start_dt.timestamp() * 1000)
                except (ValueError, AttributeError):
                    pass
            elif isinstance(start_date, datetime):
                api_kwargs["since"] = int(start_date.timestamp() * 1000)

        # Set Suunto-specific defaults
        if "limit" not in api_kwargs:
            api_kwargs["limit"] = 100

        response = self.get_workouts_from_api(db, user_id, **api_kwargs)
        workouts_data = response.get("payload", [])
        workouts = [SuuntoWorkoutJSON(**w) for w in workouts_data]

        for workout in workouts:
            # Save device/data source info if available
            if workout.gear:
                device_name = workout.gear.displayName or workout.gear.name
                self.data_source_repo.ensure_data_source(
                    db,
                    user_id=user_id,
                    provider=ProviderName.SUUNTO,
                    device_model=device_name,
                    software_version=workout.gear.swVersion,
                    source=self.provider_name,
                )

        count = 0
        for record, details in self._build_bundles(workouts, user_id):
            created_record = event_record_service.create(db, record)
            detail_for_record = details.model_copy(update={"record_id": created_record.id})
            event_record_service.create_detail(db, detail_for_record)
            count += 1
            # Backfilled workouts need their track too, otherwise only workouts
            # that arrive by webhook after connecting are ever mappable. Gated on
            # ingest_workout_samples because it costs one extra API call per
            # workout, and Suunto's developer tier allows only 200 calls/week.
            if settings.ingest_workout_samples and record.external_id:
                self.import_workout_fit(db, user_id, record.external_id)

        return count

    def get_workout_detail(
        self,
        db: DbSession,
        user_id: UUID,
        workout_key: str,
        extensions: list[str] | None = None,
    ) -> dict:
        """Get detailed workout data from Suunto API.

        `extensions` maps to the `?extensions=Foo,Bar` query parameter; requested
        blocks land in `payload.extensions[]`.
        """
        headers = self._get_suunto_headers()
        params: dict[str, str] | None = None
        if extensions:
            params = {"extensions": ",".join(extensions)}
        return self._make_api_request(db, user_id, f"/v3/workouts/{workout_key}", params=params, headers=headers)

    # ------------------------------------------------------------------
    # FIT export
    #
    # Suunto's workout JSON carries summaries only. The FIT export is the only
    # place per-sample data lives — GPS track above all — so without it a Suunto
    # workout can never be drawn on a map.
    # ------------------------------------------------------------------

    def fetch_workout_fit(self, db: DbSession, user_id: UUID, workout_key: str) -> bytes:
        """Download the raw FIT export for one workout.

        Requires both the OAuth bearer token and the Ocp-Apim-Subscription-Key
        header, so it cannot go through ``_make_api_request`` (which expects JSON).
        """
        url = f"{self.api_base_url}{FIT_EXPORT_ENDPOINT.format(workout_key=workout_key)}"
        return download_binary_content(
            db=db,
            user_id=user_id,
            connection_repo=self.connection_repo,
            oauth=self.oauth,
            provider_name=self.provider_name,
            url=url,
            headers=self._get_suunto_headers(),
        )

    def import_workout_fit(self, db: DbSession, user_id: UUID, workout_key: str) -> int:
        """Fetch, parse and persist the FIT export for one workout.

        Mirrors the Garmin activityFiles path: download -> optional S3 archive ->
        parse_fit_file -> segments/zones onto workout_details -> samples into
        data_point_series. Returns the number of samples written.

        Never raises. A workout whose FIT is missing or unparseable is still worth
        keeping, so every failure is logged and reported as zero samples.
        """
        try:
            fit_bytes = self.fetch_workout_fit(db, user_id, workout_key)
        except Exception as e:
            log_structured(
                self.logger,
                "warning",
                "Failed to download Suunto FIT file",
                provider=self.provider_name,
                task="import_workout_fit",
                user_id=str(user_id),
                workout_key=str(workout_key),
                error=str(e),
            )
            return 0

        store_fit_file(
            provider=self.provider_name,
            fit_bytes=fit_bytes,
            user_id=str(user_id),
            activity_id=str(workout_key),
        )

        try:
            fit_result = parse_fit_file(fit_bytes, user_id, source=self.provider_name)
        except Exception as e:
            log_structured(
                self.logger,
                "warning",
                "Failed to parse Suunto FIT file",
                provider=self.provider_name,
                task="import_workout_fit",
                user_id=str(user_id),
                workout_key=str(workout_key),
                error=str(e),
            )
            return 0

        if fit_result.segments or fit_result.hr_zones or fit_result.power_zones:
            self._save_fit_workout_fields(db, user_id, str(workout_key), fit_result)

        samples_written = 0
        # Same gate as Garmin: per-sample rows are large, so a deployment opts in.
        if settings.ingest_workout_samples and fit_result.samples:
            samples_written = int(self.data_point_repo.bulk_create(db, fit_result.samples))

        log_structured(
            self.logger,
            "info",
            "Parsed Suunto FIT file",
            provider=self.provider_name,
            task="import_workout_fit",
            user_id=str(user_id),
            workout_key=str(workout_key),
            segments=len(fit_result.segments),
            samples=samples_written,
        )
        return samples_written

    def _save_fit_workout_fields(
        self,
        db: DbSession,
        user_id: UUID,
        workout_key: str,
        fit_result: Any,
    ) -> None:
        """Write segments and zones from the FIT onto the workout's detail row.

        No-op when the event_record does not exist yet — callers import the FIT
        after saving the workout, so a miss means the workout itself failed.
        """
        record = self.workout_repo.get_by_external_id(db, user_id, workout_key, source=self.provider_name)
        if record is None:
            log_structured(
                self.logger,
                "warning",
                "No event_record for Suunto workout — FIT workout fields not saved",
                provider=self.provider_name,
                task="_save_fit_workout_fields",
                user_id=str(user_id),
                workout_key=workout_key,
            )
            return

        fields: dict[str, Any] = {"segments": fit_result.segments}
        if fit_result.hr_zones is not None:
            fields["hr_zones"] = fit_result.hr_zones.model_dump()
        if fit_result.power_zones is not None:
            fields["power_zones"] = fit_result.power_zones.model_dump()
        try:
            self.event_record_detail_repo.update_workout_fields(db, record.id, fields)
        except Exception as e:
            log_structured(
                self.logger,
                "warning",
                "Failed to save Suunto FIT workout fields",
                provider=self.provider_name,
                task="_save_fit_workout_fields",
                user_id=str(user_id),
                workout_key=workout_key,
                error=str(e),
            )

    def process_push_activity(self, db: DbSession, user_id: UUID, raw_workout: Any) -> UUID | None:
        """Save a single workout received via the live webhook push path.

        Mirrors the load_data backfill path: builds the record + detail bundle
        and inserts both via event_record_service. create_detail schedules the
        after_commit listener that fires the outgoing webhook (workout.created),
        so consumers subscribed via Svix receive the new workout.

        Returns the created event_record id, or None when the bundle was empty.
        """
        if isinstance(raw_workout, dict):
            raw_workout = SuuntoWorkoutJSON(**raw_workout)

        gear = raw_workout.gear or raw_workout.gear_from_summary_extension
        if gear:
            device_name = gear.displayName or gear.name
            self.data_source_repo.ensure_data_source(
                db,
                user_id=user_id,
                provider=ProviderName.SUUNTO,
                device_model=device_name,
                software_version=gear.swVersion,
                source=self.provider_name,
            )

        for record, detail in self._build_bundles([raw_workout], user_id):
            created_record = event_record_service.create(db, record)
            detail_for_record = detail.model_copy(update={"record_id": created_record.id})
            event_record_service.create_detail(db, detail_for_record)
            return created_record.id

        return None

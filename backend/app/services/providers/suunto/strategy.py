from app.schemas.auth import LiveSyncMode
from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.suunto.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.suunto.data_247 import Suunto247Data
from app.services.providers.suunto.oauth import SuuntoOAuth
from app.services.providers.suunto.webhook_handler import SuuntoWebhookHandler
from app.services.providers.suunto.workouts import SuuntoWorkouts


class SuuntoStrategy(BaseProviderStrategy):
    """Suunto provider implementation."""

    def __init__(self):
        super().__init__()
        self.oauth = SuuntoOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.workouts = SuuntoWorkouts(
            workout_repo=self.workout_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.data_247 = Suunto247Data(
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.webhooks = SuuntoWebhookHandler(
            suunto_workouts=self.workouts,
            suunto_247=self.data_247,
        )

    @property
    def name(self) -> str:
        return "suunto"

    @property
    def api_base_url(self) -> str:
        return "https://cloudapi.suunto.com"

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            health_scores=HEALTH_SCORES,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Historical sync uses REST (rest_pull); live data via webhooks (webhook_stream).
        #
        # max_historical_days is a budget, not a platform limit. Suunto's Developer
        # tier allows 200 calls a week in total, and pulling the FIT export costs one
        # call per workout on top of the listing. A 90-day backfill for a single
        # active athlete is enough to spend most of that week on the first connect.
        return ProviderCapabilities(rest_pull=True, webhook_stream=True, max_historical_days=30)

    @property
    def default_live_sync_mode(self) -> LiveSyncMode:
        """Webhook, not pull — Suunto's notifications carry their payload.

        The base rule prefers PULL whenever rest_pull exists, which is right for a
        provider whose webhook only pings and leaves the data to be fetched. Suunto
        does not ping: WORKOUT_CREATED and the 24/7 events arrive complete, so a
        periodic pull re-asks for what already came in and almost always returns
        nothing new. Against a 200-call weekly budget an hourly poll per user
        exhausts the quota in a day and buys no freshness at all.

        REST stays available for historical backfill, which _include_in_periodic_pull
        admits regardless of this mode.
        """
        return LiveSyncMode.WEBHOOK

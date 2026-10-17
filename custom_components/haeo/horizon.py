"""Horizon manager for HAEO forecast time windows.

This module provides the HorizonManager class which manages the forecast time horizon
used by all input entities. It is a pure Python class (not an entity) that can be
created early in the setup process before any platforms are loaded.

The HorizonManager:
- Computes forecast timestamps based on tier configuration
- Uses dynamic time alignment when a preset is selected
- Schedules updates at period boundaries
- Provides callbacks for dependent components to subscribe to horizon changes
"""

import asyncio
from collections.abc import Callable
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from custom_components.haeo.core.data.forecast_times import (
    floor_timestamp,
    generate_forecast_timestamps,
    tiers_to_periods_seconds,
)


class HorizonManager:
    """Manager for the forecast time horizon.

    This class:
    - Provides forecast timestamps for all input entities to use
    - Schedules updates at period boundaries
    - Notifies subscribers when the horizon changes

    Unlike HaeoHorizonEntity, this is a pure Python object that can be
    created before any entity platforms are set up.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the horizon manager."""
        self._hass = hass
        self._config_entry = config_entry

        self._periods_seconds: list[int] = []
        self._smallest_period = 60

        # Timer for next update
        self._unsub_timer: CALLBACK_TYPE | None = None

        # Subscribers to horizon changes
        self._subscribers: list[tuple[Callable[[], asyncio.Future[None] | None], bool]] = []

        # Current forecast timestamps (cached)
        self._forecast_timestamps: tuple[float, ...] = ()

        # Initialize timestamps
        self._update_timestamps()

    def _update_timestamps(self) -> None:
        """Update the cached forecast timestamps."""
        now = dt_util.now()
        periods_for_min = tiers_to_periods_seconds(self._config_entry.data, start_time=now)
        if periods_for_min:
            self._smallest_period = min(periods_for_min)
        start_ts = floor_timestamp(now.timestamp(), self._smallest_period)
        start_dt = datetime.fromtimestamp(start_ts, tz=now.tzinfo)
        self._periods_seconds = tiers_to_periods_seconds(self._config_entry.data, start_time=start_dt)
        self._forecast_timestamps = generate_forecast_timestamps(self._periods_seconds, start_ts)

    def start(self) -> Callable[[], None]:
        """Start the scheduled updates.

        Call this after the manager is fully initialized and ready to
        receive timer callbacks.

        Returns:
            A stop function that can be passed to async_on_unload.

        """
        self._schedule_next_update()
        return self.stop

    def stop(self) -> None:
        """Stop scheduled updates and clean up resources."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        # Clear all subscribers to prevent stale callbacks during reload
        self._subscribers.clear()

    def pause(self) -> None:
        """Pause scheduled updates without clearing subscribers.

        Used when auto-optimize is disabled to freeze the horizon in time.
        Call resume() to restart updates.
        """
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    def resume(self) -> None:
        """Resume scheduled updates after being paused.

        Updates timestamps to current time, notifies all subscribers,
        and restarts the update timer.
        """
        self.advance()
        self._schedule_next_update()

    def advance(self, *, manual: bool = False) -> tuple[asyncio.Future[None], ...]:
        """Advance once without scheduling, returning subscriber-owned refresh tasks."""
        self._update_timestamps()
        return tuple(
            task
            for subscriber, manual_only in self._subscribers
            if (manual or not manual_only) and (task := subscriber()) is not None
        )

    def _schedule_next_update(self) -> None:
        """Schedule the next horizon update at the next period boundary."""
        self.pause()
        now = dt_util.utcnow()
        epoch_seconds = now.timestamp()

        # Calculate next period boundary
        current_boundary = floor_timestamp(epoch_seconds, self._smallest_period)
        next_boundary = current_boundary + self._smallest_period

        # Convert to datetime for scheduling
        next_update_time = datetime.fromtimestamp(next_boundary, tz=dt_util.UTC)

        self._unsub_timer = async_track_point_in_time(
            self._hass,
            self._async_scheduled_update,
            next_update_time,
        )

    @callback
    def _async_scheduled_update(self, _now: datetime) -> None:
        """Handle scheduled update when period boundary is reached."""
        self.advance()
        self._schedule_next_update()

    def subscribe(
        self, callback_fn: Callable[[], asyncio.Future[None] | None], *, manual_only: bool = False
    ) -> Callable[[], None]:
        """Subscribe to horizon changes.

        Args:
            callback_fn: Function returning an already-started refresh future, or None.
            manual_only: Only notify during manual preparation, for scalar input refreshes.

        Returns:
            Unsubscribe function to remove the subscription

        """
        subscription = (callback_fn, manual_only)
        self._subscribers.append(subscription)

        def unsubscribe() -> None:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

        return unsubscribe

    def get_forecast_timestamps(self) -> tuple[float, ...]:
        """Get the current forecast timestamps as epoch values.

        Returns boundary timestamps for the horizon (n_periods + 1 values).
        """
        return self._forecast_timestamps

    @property
    def periods_seconds(self) -> list[int]:
        """Get the period durations in seconds."""
        return self._periods_seconds

    @property
    def smallest_period(self) -> int:
        """Get the smallest period duration in seconds."""
        return self._smallest_period

    @property
    def period_count(self) -> int:
        """Get the number of periods in the horizon."""
        return len(self._periods_seconds)

    @property
    def current_start_time(self) -> datetime | None:
        """Get the current period start time as a datetime."""
        if self._forecast_timestamps:
            local_tz = dt_util.get_default_time_zone()
            return datetime.fromtimestamp(self._forecast_timestamps[0], tz=local_tz)
        return None


__all__ = ["HorizonManager"]

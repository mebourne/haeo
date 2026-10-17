"""Optimization snapshots and manual actions through real Home Assistant setup and entities."""

import asyncio
from datetime import datetime, timedelta
import threading

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.haeo.core.model import Network
from custom_components.haeo.core.state import StateMachine
from custom_components.haeo.sensor_utils import get_output_sensors
from tools.sim_hass import setup_haeo_entry


def hub_config() -> dict[str, JsonValueType]:
    """Build editable/driven inputs using the same units and topology as a real hub."""
    participants: dict[str, JsonValueType] = {
        "AC bus": {"name": "AC bus", "element_type": "node", "role": {"is_source": False, "is_sink": False}},
        "Storage": {
            "name": "Storage",
            "element_type": "battery",
            "connection": {"type": "connection_target", "value": "Converter"},
            "storage": {
                "capacity": {"type": "constant", "value": 40.3},
                "initial_charge_percentage": {"type": "entity", "value": ["sensor.observed_soc"]},
            },
            "limits": {
                "min_charge_percentage": {"type": "constant", "value": 30},
                "max_charge_percentage": {"type": "constant", "value": 95},
            },
            "undercharge": {
                "percentage": {"type": "constant", "value": 10},
                "cost": {"type": "constant", "value": 4},
            },
            "overcharge": {
                "percentage": {"type": "constant", "value": 100},
                "cost": {"type": "constant", "value": 0.02},
            },
            "partitioning": {},
            "power_limits": {
                "max_power_source_target": {"type": "constant", "value": 24},
                "max_power_target_source": {"type": "constant", "value": 21},
            },
            "efficiency": {
                "efficiency_source_target": {"type": "constant", "value": 95},
                "efficiency_target_source": {"type": "constant", "value": 91},
            },
            "pricing": {"salvage_value": {"type": "constant", "value": 0.5}},
        },
        "Market": {
            "name": "Market",
            "element_type": "grid",
            "connection": {"type": "connection_target", "value": "AC bus"},
            "power_limits": {
                "max_power_source_target": {"type": "constant", "value": 10},
                "max_power_target_source": {"type": "constant", "value": 10},
            },
            "pricing": {
                "price_source_target": {"type": "entity", "value": ["sensor.import_tariff"]},
                "price_target_source": {"type": "entity", "value": ["sensor.export_tariff"]},
            },
        },
        "Converter": {
            "name": "Converter",
            "element_type": "inverter",
            "connection": {"type": "connection_target", "value": "AC bus"},
            "power_limits": {
                "max_power_source_target": {"type": "constant", "value": 9.999},
                "max_power_target_source": {"type": "constant", "value": 9.999},
            },
            "efficiency": {
                "efficiency_source_target": {"type": "constant", "value": 95},
                "efficiency_target_source": {"type": "constant", "value": 93},
            },
        },
        "House": {
            "name": "House",
            "element_type": "load",
            "connection": {"type": "connection_target", "value": "AC bus"},
            "forecast": {"forecast": {"type": "constant", "value": 0.6}},
            "curtailment": {},
            "pricing": {},
        },
        "Idle": {
            "name": "Idle",
            "element_type": "load",
            "connection": {"type": "connection_target", "value": "Converter"},
            "forecast": {"forecast": {"type": "constant", "value": 0.165}},
            "curtailment": {},
            "pricing": {},
        },
        "Valuations": {
            "name": "Valuations",
            "element_type": "policy",
            "rules": [
                {
                    "name": "Wear",
                    "enabled": True,
                    "source": ["Storage"],
                    "target": ["Market"],
                    "price": {"type": "constant", "value": 0.08},
                },
                {
                    "name": "Charge",
                    "enabled": True,
                    "target": ["Storage"],
                    "price": {"type": "constant", "value": -0.001},
                },
            ],
        },
    }
    return {
        "integration_type": "hub",
        "common": {"name": "Optimizer", "horizon_preset": "custom"},
        "tiers": {"tier_1_count": 8, "tier_1_duration": 60},
        "advanced": {"advanced_mode": True, "debounce_seconds": 2},
        "participants": participants,
    }


def set_inputs(hass: HomeAssistant, *, soc: float = 30, export: float = 21) -> None:
    """Supply observed state and native HAEO-format forecast tariffs."""
    start = dt_util.utcnow().replace(second=0, microsecond=0)
    for entity_id, values in (
        ("sensor.import_tariff", [max(4, export + 1)] + [4] * 4 + [0.09] * 3),
        ("sensor.export_tariff", [export] + [0.15] * 7),
    ):
        hass.states.async_set(
            entity_id,
            str(values[0]),
            {
                "unit_of_measurement": "$/kWh",
                "forecast": [
                    {"time": (start + timedelta(hours=t)).isoformat(), "value": price} for t, price in enumerate(values)
                ],
            },
        )
    hass.states.async_set("sensor.observed_soc", str(soc), {"unit_of_measurement": "%"})


@freeze_time("2026-10-16T00:00:00Z")
async def test_changed_inputs_publish_snapshot_and_queue_refresh(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain outputs, publish a coherent starting snapshot, then replan queued input changes."""
    set_inputs(hass)
    entry = await setup_haeo_entry(hass, hub_config())
    coordinator = entry.runtime_data.coordinator
    published_outputs = {
        entity_id: output
        for entity_id, output in get_output_sensors(hass, entry).items()
        if entity_id.startswith("sensor.")
    }
    set_inputs(hass, soc=35, export=8)
    await hass.async_block_till_done()
    solve_started = asyncio.Event()
    release = threading.Event()
    optimize = Network.optimize

    def held_optimize(network: Network) -> float:
        hass.loop.call_soon_threadsafe(solve_started.set)
        assert release.wait(2)
        return optimize(network)

    monkeypatch.setattr(Network, "optimize", held_optimize)
    refresh = hass.async_create_task(coordinator.async_refresh())
    try:
        await asyncio.wait_for(solve_started.wait(), 2)
        soc_changed, price_changed = asyncio.Event(), asyncio.Event()
        unsubscribers = [
            entry.runtime_data.input_stores[key].add_listener(event.set)
            for key, event in (
                (("Storage", ("storage", "initial_charge_percentage")), soc_changed),
                (("Market", ("pricing", "price_target_source")), price_changed),
            )
        ]
        set_inputs(hass, soc=20, export=0.15)
        # Process real source events while the executor is held. The solver and
        # output entities are real, and must not read these newer store values.
        await asyncio.wait_for(asyncio.gather(soc_changed.wait(), price_changed.wait()), 2)
        for unsubscribe in unsubscribers:
            unsubscribe()
        outputs = get_output_sensors(hass, entry)
        assert {entity_id: outputs[entity_id] for entity_id in published_outputs} == published_outputs
    finally:
        release.set()
        await refresh
    outputs = get_output_sensors(hass, entry)
    soc_forecast = outputs["sensor.storage_state_of_charge"]["attributes"].get("forecast")
    assert isinstance(soc_forecast, list)
    assert float(soc_forecast[0]["value"]) == 35
    assert coordinator.data.context.source_states["sensor.observed_soc"].state == "35"
    assert coordinator.data.context.source_states["sensor.export_tariff"].state == "8"

    monkeypatch.setattr(Network, "optimize", optimize)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    outputs = get_output_sensors(hass, entry)
    assert float(outputs["sensor.market_export_power"]["state"]) == 0
    soc_forecast = outputs["sensor.storage_state_of_charge"]["attributes"].get("forecast")
    assert isinstance(soc_forecast, list)
    assert float(soc_forecast[0]["value"]) == 20


async def _setup_manual_hub(hass: HomeAssistant, *, horizon_preset: str = "custom") -> MockConfigEntry:
    """Set up a paused minute-resolution hub with the requested horizon preset."""
    await hass.config.async_set_time_zone("Australia/Adelaide")
    set_inputs(hass)
    config = hub_config()
    config["tiers"] = {"tier_1_count": 8, "tier_1_duration": 1, "tier_2_count": 8, "tier_2_duration": 60}
    entry = await setup_haeo_entry(hass, config)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "common": {"name": "Optimizer", "horizon_preset": horizon_preset}},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entry.runtime_data.auto_optimize_switch.entity_id}, blocking=True
    )
    await hass.async_block_till_done()
    return entry


async def test_manual_optimization_advances_aligned_plan_without_automatic_runs(hass: HomeAssistant) -> None:
    """Manual actions refresh aligned snapshots; source and time changes alone leave outputs held."""
    with freeze_time("2026-10-17T02:00:00Z") as clock:
        entry = await _setup_manual_hub(hass, horizon_preset="3_days")
        coordinator = entry.runtime_data.coordinator
        previous = coordinator.data
        clock.tick(timedelta(minutes=7, seconds=22))
        set_inputs(hass, soc=57, export=8)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.data is previous

        await hass.services.async_call("haeo", "optimize", {"config_entry": entry.entry_id}, blocking=True)
        expected = datetime.fromisoformat("2026-10-17T12:37:00+10:30")
        assert coordinator.data.context.horizon_start == expected
        assert coordinator.data.context.source_states["sensor.observed_soc"].state == "57"
        assert coordinator.data.context.source_states["sensor.export_tariff"].state == "8"
        outputs = get_output_sensors(hass, entry)
        forecast = outputs["sensor.storage_state_of_charge"]["attributes"].get("forecast")
        assert isinstance(forecast, list)
        assert forecast[0]["time"] == expected
        assert forecast[0]["value"] == 57
        manager = entry.runtime_data.horizon_manager
        boundaries = manager.get_forecast_timestamps()
        for index, duration in enumerate(manager.periods_seconds):
            if duration == 3600:
                assert datetime.fromtimestamp(boundaries[index], tz=expected.tzinfo).minute == 0
        assert boundaries[-1] - boundaries[0] == 72 * 3600
        assert not coordinator.auto_optimize_enabled

        previous = coordinator.data
        clock.tick(timedelta(minutes=11))
        set_inputs(hass, soc=20, export=0.15)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.data is previous
        assert (
            get_output_sensors(hass, entry)["sensor.storage_state_of_charge"]
            == outputs["sensor.storage_state_of_charge"]
        )
        await hass.services.async_call("haeo", "optimize", {"config_entry": entry.entry_id}, blocking=True)
        assert coordinator.data.context.horizon_start == datetime.fromisoformat("2026-10-17T12:48:00+10:30")
        assert coordinator.data.context.source_states["sensor.observed_soc"].state == "20"


async def test_manual_optimization_awaits_input_load_and_reports_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The action waits for fresh input loading, reports unavailable data, and recovers on a subsequent call."""
    with freeze_time("2026-10-17T02:00:00Z") as clock:
        entry = await _setup_manual_hub(hass)
        coordinator = entry.runtime_data.coordinator
        previous = coordinator.data
        store = entry.runtime_data.input_stores[("Market", ("pricing", "price_target_source"))]
        load = store.async_load
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_load(sm: StateMachine) -> bool:
            started.set()
            await release.wait()
            return await load(sm)

        monkeypatch.setattr(store, "async_load", delayed_load)
        clock.tick(timedelta(minutes=7))
        action = hass.async_create_task(
            hass.services.async_call("haeo", "optimize", {"config_entry": entry.entry_id}, blocking=True)
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert not action.done()
            assert coordinator.data is previous
            hass.states.async_set("sensor.export_tariff", "unavailable")
        finally:
            release.set()
        with pytest.raises(HomeAssistantError, match="unavailable inputs"):
            await action
        assert coordinator.data is previous
        assert get_output_sensors(hass, entry)["sensor.market_export_power"]["state"] == "unavailable"

        set_inputs(hass, soc=43, export=8)
        await hass.services.async_call("haeo", "optimize", {"config_entry": entry.entry_id}, blocking=True)
        assert coordinator.last_update_success
        assert coordinator.data.context.horizon_start == datetime.fromisoformat("2026-10-17T12:37:00+10:30")
        assert coordinator.data.context.source_states["sensor.export_tariff"].state == "8"
        assert coordinator.data.context.source_states["sensor.observed_soc"].state == "43"


async def test_disabling_auto_discards_queued_automatic_but_not_manual_runs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active solve completes, queued automatic work is skipped after disable, and queued manual work runs fresh."""
    with freeze_time("2026-10-17T02:00:00Z") as clock:
        entry = await _setup_manual_hub(hass)
        coordinator = entry.runtime_data.coordinator
        started, release = asyncio.Event(), threading.Event()
        optimize = Network.optimize
        runs: list[str] = []

        def held_optimize(network: Network) -> float:
            runs.append("native")
            hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(2)
            return optimize(network)

        monkeypatch.setattr(Network, "optimize", held_optimize)
        published: list[str] = []
        unsubscribe = coordinator.async_add_listener(
            lambda: published.append(coordinator.data.context.source_states["sensor.observed_soc"].state)
        )
        first = hass.async_create_task(coordinator.async_run_optimization())
        try:
            await asyncio.wait_for(started.wait(), 2)
            switch = entry.runtime_data.auto_optimize_switch.entity_id
            await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
            await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
            clock.tick(timedelta(minutes=7))
            set_inputs(hass, soc=20, export=0.15)
            second = hass.async_create_task(coordinator.async_run_optimization())
            await asyncio.sleep(0)
            assert len(runs) == 1
        finally:
            release.set()
            await first
        await second
        await hass.async_block_till_done()
        unsubscribe()
        assert len(runs) == 2
        assert published == ["30", "20"]
        assert coordinator.last_update_success
        assert coordinator.data.context.horizon_start == datetime.fromisoformat("2026-10-17T12:37:00+10:30")
        previous = coordinator.data
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert coordinator.data is previous


async def test_cancelled_manual_solve_keeps_worker_serialized(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated cancellation cannot release a running worker's network to the next manual optimization."""
    entry = await _setup_manual_hub(hass)
    coordinator = entry.runtime_data.coordinator
    previous = coordinator.data
    started, release = asyncio.Event(), threading.Event()
    optimize = Network.optimize
    runs: list[str] = []

    def held_optimize(network: Network) -> float:
        runs.append("native")
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        return optimize(network)

    monkeypatch.setattr(Network, "optimize", held_optimize)
    first = hass.async_create_task(coordinator.async_run_optimization())
    try:
        await asyncio.wait_for(started.wait(), 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        set_inputs(hass, soc=42, export=0.15)
        second = hass.async_create_task(coordinator.async_run_optimization())
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        assert len(runs) == 1
        assert coordinator.data is previous
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
    await second
    assert len(runs) == 2
    assert coordinator.last_update_success
    assert coordinator.data.context.source_states["sensor.observed_soc"].state == "42"


@pytest.mark.parametrize("interruption", ["cancel", "timeout", "unload"])
async def test_manual_input_preparation_interruption_never_solves_later(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """Cancellation, timeout and unload abandon preparation without later publishing or starting a solve."""
    entry = await _setup_manual_hub(hass)
    coordinator = entry.runtime_data.coordinator
    previous = coordinator.data
    store = entry.runtime_data.input_stores[("Market", ("pricing", "price_target_source"))]
    load = store.async_load
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_load(sm: StateMachine) -> bool:
        started.set()
        await release.wait()
        return await load(sm)

    def unexpected_solve(network: Network) -> float:
        pytest.fail("Interrupted preparation must not start a solver")

    monkeypatch.setattr(store, "async_load", delayed_load)
    monkeypatch.setattr(Network, "optimize", unexpected_solve)
    if interruption == "timeout":
        monkeypatch.setattr("custom_components.haeo.coordinator.coordinator.INPUT_ENTITY_READY_TIMEOUT", 0.01)
    action = hass.async_create_task(coordinator.async_run_optimization())
    try:
        await asyncio.wait_for(started.wait(), 2)
        if interruption == "cancel":
            action.cancel()
            with pytest.raises(asyncio.CancelledError):
                await action
        elif interruption == "timeout":
            with pytest.raises(HomeAssistantError, match="Timed out preparing inputs"):
                await action
        else:
            assert await hass.config_entries.async_unload(entry.entry_id)
            with pytest.raises(HomeAssistantError):
                await action
    finally:
        release.set()
    await hass.async_block_till_done()
    assert coordinator.data is previous


async def test_auto_switch_cancels_cooldown_and_resumes_current_horizon(hass: HomeAssistant) -> None:
    """Re-enabling catches up and resumes automatic updates; disabling drops a pending cooldown refresh."""
    with freeze_time("2026-10-17T02:00:00Z") as clock:
        entry = await _setup_manual_hub(hass)
        coordinator = entry.runtime_data.coordinator
        switch = entry.runtime_data.auto_optimize_switch.entity_id
        clock.tick(timedelta(minutes=5))
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
        await hass.async_block_till_done()
        assert coordinator.data.context.horizon_start == datetime.fromisoformat("2026-10-17T12:35:00+10:30")
        previous = coordinator.data
        set_inputs(hass, soc=42, export=0.15)
        await hass.async_block_till_done()
        assert coordinator.data is previous
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
        clock.tick(timedelta(seconds=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.data is previous
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
        await hass.async_block_till_done()
        assert coordinator.data.context.source_states["sensor.observed_soc"].state == "42"
        clock.tick(timedelta(minutes=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.data.context.horizon_start == datetime.fromisoformat("2026-10-17T12:36:00+10:30")

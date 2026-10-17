"""Optimization snapshots through real Home Assistant setup and entities."""

import asyncio
from datetime import timedelta
import threading

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haeo.core.model import Network
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

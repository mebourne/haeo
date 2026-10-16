"""Reserve policy through real Home Assistant setup, entities and reloads."""

import asyncio
from datetime import timedelta
import threading

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haeo.core.adapters.reserve import ReservePlanner, ReserveResult
from custom_components.haeo.core.const import HUB_SECTION_RESERVE_POLICY
from custom_components.haeo.sensor_utils import get_output_sensors
from tools.sim_hass import setup_haeo_entry


def reserve_hub_config() -> dict[str, JsonValueType]:
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
        HUB_SECTION_RESERVE_POLICY: {"enabled": True},
        "participants": participants,
    }


def set_reserve_inputs(hass: HomeAssistant, *, soc: float = 30, export: float = 21) -> None:
    """Supply observed state and native HAEO-format forecast tariffs."""
    start = dt_util.utcnow().replace(second=0, microsecond=0)
    for entity_id, values in (
        ("sensor.import_tariff", [4] * 5 + [0.09] * 3),
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


async def test_reserve_setup_reload_and_opt_out(hass: HomeAssistant) -> None:
    """Existing forecasts publish safely; reload is stateless and opt-out is reversible."""
    set_reserve_inputs(hass)
    # Obsolete cutoffs, deadlines and recovery margins must not restrict a solve.
    config: dict[str, JsonValueType] = {
        **reserve_hub_config(),
        HUB_SECTION_RESERVE_POLICY: {
            "enabled": True,
            "margin_percentage": 20,
            "time_limit": 1e-9,
            "import_price": -100,
        },
    }
    entry = await setup_haeo_entry(hass, config)
    outputs = get_output_sensors(hass, entry)
    expected_export = 40.3 * 0.20 * 0.95 * 0.95 - 0.6 - 0.165 * 0.95
    assert float(outputs["sensor.market_export_power"]["state"]) == pytest.approx(expected_export, abs=0.01)
    soc_forecast = outputs["sensor.storage_state_of_charge"]["attributes"].get("forecast")
    assert isinstance(soc_forecast, list)
    # The release publishes all nine boundaries for eight hourly periods.
    assert len(soc_forecast) == 9
    # Cancel the spike and observe partial execution, not the planned endpoint.
    set_reserve_inputs(hass, soc=25, export=0.15)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_refresh()
    outputs = get_output_sensors(hass, entry)
    assert float(outputs["sensor.market_export_power"]["state"]) == 0
    imports = outputs["sensor.market_import_power"]["attributes"].get("forecast")
    assert isinstance(imports, list)
    assert max(float(p["value"]) for p in imports[:5]) == 0
    # A fresh coordinator must publish the same economic household supply.
    entity_ids = set(outputs)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert set(get_output_sensors(hass, entry)) == entity_ids
    assert float(get_output_sensors(hass, entry)["sensor.market_import_power"]["state"]) == 0

    # The stored hourly cost is unchanged; switching off restores the native LP.
    hass.config_entries.async_update_entry(entry, data={**entry.data, HUB_SECTION_RESERVE_POLICY: {"enabled": False}})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(get_output_sensors(hass, entry)["sensor.market_import_power"]["state"]) > 0
    # The native LP additionally supplies dual-derived load summary sensors.
    assert entity_ids <= set(get_output_sensors(hass, entry))
    hass.config_entries.async_update_entry(entry, data={**entry.data, HUB_SECTION_RESERVE_POLICY: {"enabled": True}})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(get_output_sensors(hass, entry)["sensor.market_import_power"]["state"]) == 0

    # Failed fresh solves must not expose last-success scalar states as current.
    hass.states.async_set("sensor.observed_soc", "5", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_refresh()
    outputs = get_output_sensors(hass, entry)
    assert outputs["sensor.market_export_power"]["state"] == "unavailable"
    assert outputs["sensor.storage_state_of_charge"]["state"] == "unavailable"
    assert "forecast" not in outputs["sensor.market_export_power"]["attributes"]


async def test_changed_inputs_discard_in_flight_result_and_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canceled spike must not publish a stale sell plan or require another price update."""
    set_reserve_inputs(hass)
    entry = await setup_haeo_entry(hass, reserve_hub_config())
    coordinator = entry.runtime_data.coordinator
    solve_finished = asyncio.Event()
    release = threading.Event()
    solve = ReservePlanner.solve

    def held_solve(planner: ReservePlanner) -> ReserveResult:
        result = solve(planner)
        hass.loop.call_soon_threadsafe(solve_finished.set)
        assert release.wait(2)
        return result

    monkeypatch.setattr(ReservePlanner, "solve", held_solve)
    refresh = hass.async_create_task(coordinator.async_refresh())
    await asyncio.wait_for(solve_finished.wait(), 2)
    inputs_changed = asyncio.Event()
    unsubscribe = entry.runtime_data.input_stores[("Market", ("pricing", "price_target_source"))].add_listener(
        inputs_changed.set
    )
    set_reserve_inputs(hass, export=0.15)
    # Let the real input stores process their source-state events before returning
    # the old result. The solver itself and output publication are not mocked.
    await asyncio.wait_for(inputs_changed.wait(), 2)
    unsubscribe()
    release.set()
    await refresh
    outputs = get_output_sensors(hass, entry)
    assert outputs["sensor.market_export_power"]["state"] == "unavailable"
    monkeypatch.setattr(ReservePlanner, "solve", solve)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    outputs = get_output_sensors(hass, entry)
    assert float(outputs["sensor.market_export_power"]["state"]) == 0

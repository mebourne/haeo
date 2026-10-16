"""Release setup after an interrupted or completed development migration."""

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.haeo.const import DOMAIN
from custom_components.haeo.flows import HORIZON_PRESETS


@pytest.mark.parametrize("migration_state", ["release", "interrupted", "completed", "rollback_retry"])
@pytest.mark.timeout(30)
async def test_release_setup_preserves_switchboard(hass: HomeAssistant, migration_state: str) -> None:
    """Set up and reload native forecasts, retaining node identity and customizations after development migration."""
    tiers = {
        "tier_1_count": 3,
        "tier_1_duration": 1,
        "tier_2_count": 0,
        "tier_2_duration": 5,
        "tier_3_count": 0,
        "tier_3_duration": 30,
        "tier_4_count": 0,
        "tier_4_duration": 60,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=4 if migration_state == "completed" else 3,
        data={
            "integration_type": "hub",
            "common": {"name": "Home", "horizon_preset": "custom"},
            "advanced": {"advanced_mode": True},
            "tiers": tiers,
        },
    )
    if migration_state == "completed":
        entry = MockConfigEntry(
            domain=DOMAIN,
            version=1,
            minor_version=4,
            data={
                "integration_type": "hub",
                "common": {"name": "Home", "horizon": {"type": "preset", "value": "3_days"}},
                "advanced": {"advanced_mode": True},
            },
        )
    entry.add_to_hass(hass)
    participants = [
        {"element_type": "node", "name": "Switchboard", "role": {"is_source": False, "is_sink": False}},
        {
            "element_type": "grid",
            "name": "Grid",
            "connection": {"type": "connection_target", "value": "Switchboard"},
            "power_limits": {
                "max_power_source_target": {"type": "constant", "value": 10.0},
                "max_power_target_source": {"type": "constant", "value": 10.0},
            },
            "pricing": {
                "price_source_target": {"type": "constant", "value": 0.25},
                "price_target_source": {"type": "constant", "value": 0.05},
            },
        },
        {
            "element_type": "inverter",
            "name": "Inverter",
            "connection": {"type": "connection_target", "value": "Switchboard"},
            "power_limits": {
                "max_power_source_target": {"type": "constant", "value": 10.0},
                "max_power_target_source": {"type": "constant", "value": 10.0},
            },
            "efficiency": {},
        },
        {
            "element_type": "load",
            "name": "House",
            "connection": {"type": "connection_target", "value": "Switchboard"},
            "forecast": {"forecast": {"type": "constant", "value": 1.25}},
            "curtailment": {},
        },
    ]
    for participant in participants:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(participant),
                subentry_type=str(participant["element_type"]),
                title=str(participant["name"]),
                unique_id=None,
            ),
        )
    switchboard = next(iter(entry.subentries.values()))
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_auto_optimize", suggested_object_id="home_auto_optimize"
    )
    # Exercise explicit setup/reload without unrelated periodic replanning.
    mock_restore_cache(hass, [State("switch.home_auto_optimize", "off")])
    prefix = f"{entry.entry_id}_{switchboard.subentry_id}_"
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=switchboard.subentry_id,
        identifiers={(DOMAIN, f"{prefix}node")},
        name="Switchboard",
    )
    devices.async_update_device(device.id, name_by_user="My switchboard")
    sensor = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{prefix}node_node_power_balance",
        config_entry=entry,
        config_subentry_id=switchboard.subentry_id,
        device_id=device.id,
        suggested_object_id="my_switchboard_price",
    )
    if migration_state != "release":
        # The interrupted migration rekeyed and detached this sensor, then
        # deleted the device while removing the old node subentry.
        entities.async_update_entity(
            sensor.entity_id,
            new_unique_id=f"{prefix}junction_junction_power_balance",
            config_subentry_id=None,
            device_id=None,
        )
        hass.config_entries.async_remove_subentry(entry, switchboard.subentry_id)
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType({"element_type": "junction", "name": "Switchboard"}),
                subentry_type="junction",
                title="Switchboard",
                unique_id=None,
                subentry_id=switchboard.subentry_id,
            ),
        )
        if migration_state == "completed":
            junction_device = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                config_subentry_id=switchboard.subentry_id,
                identifiers={(DOMAIN, f"{prefix}junction")},
            )
            devices.async_update_device(junction_device.id, name_by_user="My switchboard")
        elif migration_state == "rollback_retry":
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=switchboard.data,
                    subentry_type="node",
                    title="Switchboard",
                    unique_id=None,
                ),
            )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state == ConfigEntryState.LOADED
    assert entry.subentries[switchboard.subentry_id].subentry_type == "node"
    assert sum(subentry.title == "Switchboard" for subentry in entry.subentries.values()) == 1
    assert entry.data["tiers"] == (HORIZON_PRESETS["3_days"] if migration_state == "completed" else tiers)
    restored = entities.async_get(sensor.entity_id)
    assert restored is not None
    assert restored.unique_id == f"{prefix}node_node_power_balance"
    assert restored.device_id is not None
    restored_device = devices.async_get(restored.device_id)
    assert restored_device is not None
    assert restored_device.name_by_user == "My switchboard"
    if migration_state != "completed":
        assert restored_device.id == device.id
    price_state = hass.states.get(sensor.entity_id)
    assert price_state is not None
    assert price_state.state not in {"unknown", "unavailable"}
    grid_import = hass.states.get("sensor.grid_import_power")
    assert grid_import is not None
    assert float(grid_import.state) == pytest.approx(1.25)
    assert len(grid_import.attributes["forecast"]) == (96 if migration_state == "completed" else 3)
    assert all(point["value"] == pytest.approx(1.25) for point in grid_import.attributes["forecast"])

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entities.async_get(sensor.entity_id) == restored
    reloaded_import = hass.states.get("sensor.grid_import_power")
    assert reloaded_import is not None
    assert float(reloaded_import.state) == pytest.approx(1.25)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

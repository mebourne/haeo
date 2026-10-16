"""Restore release-compatible configuration after a development upgrade."""

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.haeo.const import DOMAIN
from custom_components.haeo.core.const import CONF_ELEMENT_TYPE, CONF_HORIZON_PRESET, CONF_NAME
from custom_components.haeo.core.schema.elements import node
from custom_components.haeo.flows import HORIZON_PRESETS, HUB_SECTION_COMMON, HUB_SECTION_TIERS


def repair_release_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restore passive nodes and registry identity before release input stores load.

    This also runs for partially migrated entries whose minor version still
    matches the release, so Home Assistant would skip the migration runner.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type != "junction":
            continue
        data = {
            CONF_ELEMENT_TYPE: node.ELEMENT_TYPE,
            CONF_NAME: subentry.title,
            node.SECTION_ROLE: {node.CONF_IS_SOURCE: False, node.CONF_IS_SINK: False},
        }
        # A failed release setup can have auto-created another Switchboard.
        # Keep the original subentry and remove only its exact passive duplicate.
        for duplicate in list(entry.subentries.values()):
            if duplicate.subentry_id != subentry.subentry_id and dict(duplicate.data) == data:
                hass.config_entries.async_remove_subentry(entry, duplicate.subentry_id)
        prefix = f"{entry.entry_id}_{subentry.subentry_id}_"
        node_identifier = (DOMAIN, f"{prefix}node")
        junction_identifier = (DOMAIN, f"{prefix}junction")
        device = next(
            (
                candidate
                for candidate in dr.async_entries_for_config_entry(devices, entry.entry_id)
                if node_identifier in candidate.identifiers or junction_identifier in candidate.identifiers
            ),
            None,
        )
        if device is None:
            # Registering the original identifier restores a deleted device's
            # ID and customizations from Home Assistant's registry tombstone.
            device = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                config_subentry_id=subentry.subentry_id,
                identifiers={node_identifier},
            )
        devices.async_update_device(device.id, new_identifiers={node_identifier})
        for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
            if entity.unique_id == f"{prefix}junction_junction_power_balance":
                entities.async_update_entity(
                    entity.entity_id,
                    new_unique_id=f"{prefix}node_node_power_balance",
                    config_subentry_id=subentry.subentry_id,
                    device_id=device.id,
                )
        # Replace the same ID only after its registry repair succeeds. A retry
        # still recognizes the junction if registry restoration is interrupted.
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(data),
                subentry_type=node.ELEMENT_TYPE,
                title=subentry.title,
                unique_id=None,
                subentry_id=subentry.subentry_id,
            ),
        )

    # A completed development migration replaced preset/tiers with a choice.
    # Preserve existing custom tiers; restore a known preset only when needed.
    common = dict(entry.data.get(HUB_SECTION_COMMON, {}))
    horizon = common.get("horizon")
    if isinstance(horizon, dict) and horizon.get("type") == "preset" and horizon.get("value") in HORIZON_PRESETS:
        preset = horizon["value"]
        common.pop("horizon")
        common[CONF_HORIZON_PRESET] = preset
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                HUB_SECTION_COMMON: common,
                HUB_SECTION_TIERS: entry.data.get(HUB_SECTION_TIERS, HORIZON_PRESETS[preset]),
            },
            minor_version=3,
        )

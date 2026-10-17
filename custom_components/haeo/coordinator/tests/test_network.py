"""Tests for coordinator network utilities."""

from typing import Any

import numpy as np
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haeo.coordinator.network import (
    _MISSING,
    _build_element_updater,
    _build_policy_updater,
    _collect_policy_rules,
    _discover_setters,
    _extract_at_path,
    create_network,
)
from custom_components.haeo.core.adapters.output_utils import expect_output_data
from custom_components.haeo.core.const import CONF_ELEMENT_TYPE, CONF_NAME
from custom_components.haeo.core.model import Network
from custom_components.haeo.core.model.element import ELEMENT_POWER_BALANCE
from custom_components.haeo.core.model.elements import (
    MODEL_ELEMENT_TYPE_CONNECTION,
    MODEL_ELEMENT_TYPE_NODE,
    ModelElementConfig,
)
from custom_components.haeo.core.model.elements.connection import Connection, ConnectionElementConfig
from custom_components.haeo.core.model.elements.policy_pricing import ELEMENT_TYPE as MODEL_ELEMENT_TYPE_POLICY_PRICING
from custom_components.haeo.core.model.elements.policy_pricing import (
    PolicyPricing,
    PolicyPricingElementConfig,
    PolicyPricingTerm,
)
from custom_components.haeo.core.model.elements.segments import EfficiencySegment, PowerLimitSegment
from custom_components.haeo.core.model.elements.segments.soc_pricing import SocPricingSegment
from custom_components.haeo.core.model.network import BlendedOptions, CalibratedOptions, LexOptions, SolveOptions
from custom_components.haeo.core.schema import as_connection_target
from custom_components.haeo.core.schema.elements import ElementConfigData, ElementType
from custom_components.haeo.core.schema.elements.connection import (
    CONF_EFFICIENCY_SOURCE_TARGET,
    CONF_MAX_POWER_SOURCE_TARGET,
    CONF_PRICE_SOURCE_TARGET,
    ConnectionConfigData,
)
from custom_components.haeo.core.schema.sections.efficiency import EfficiencyData
from custom_components.haeo.core.schema.sections.power_limits import PowerLimitsData
from custom_components.haeo.core.schema.sections.pricing import PricingData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_network() -> Network:
    """Create a network with a source, sink, and connection."""
    network = Network(name="test", periods=np.array([1.0, 1.0]))
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "source", "is_source": True, "is_sink": False})
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "target", "is_source": False, "is_sink": True})
    network.add(
        {
            "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
            "name": "conn",
            "source": "source",
            "target": "target",
            "tags": {1},
            "segments": {
                "power_limit": {"segment_type": "power_limit", "max_power": np.array([10.0, 10.0])},
            },
        }
    )
    return network


def _connection_config(
    *,
    max_power: Any = None,
    price: Any = None,
    efficiency: Any = None,
) -> ConnectionConfigData:
    """Build a connection ConnectionConfigData."""
    power_limits = PowerLimitsData()
    if max_power is not None:
        power_limits[CONF_MAX_POWER_SOURCE_TARGET] = max_power
    pricing = PricingData()
    if price is not None:
        pricing[CONF_PRICE_SOURCE_TARGET] = price
    eff = EfficiencyData()
    if efficiency is not None:
        eff[CONF_EFFICIENCY_SOURCE_TARGET] = efficiency
    return ConnectionConfigData(
        element_type=ElementType.CONNECTION,
        name="conn",
        endpoints={
            "source": as_connection_target("source"),
            "target": as_connection_target("target"),
        },
        power_limits=power_limits,
        pricing=pricing,
        efficiency=eff,
    )


# ---------------------------------------------------------------------------
# _extract_at_path
# ---------------------------------------------------------------------------


def test_extract_at_path_returns_leaf_value() -> None:
    """Nested path extraction returns the leaf value."""
    config = {"a": {"b": {"c": 42}}}
    assert _extract_at_path(config, ("a", "b", "c")) == 42


def test_extract_at_path_returns_missing_for_absent_key() -> None:
    """Missing intermediate key returns the _MISSING sentinel."""
    config: dict[str, Any] = {"a": {"b": 1}}
    assert _extract_at_path(config, ("a", "x", "y")) is _MISSING


# ---------------------------------------------------------------------------
# _discover_setters
# ---------------------------------------------------------------------------


def test_discover_setters_finds_tracked_params() -> None:
    """Setters are created for TrackedParam descriptors on model elements."""
    network = _simple_network()
    conn = network.elements["conn"]
    model_config = {
        "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
        "name": "conn",
        "source": "source",
        "target": "target",
        "segments": {
            "power_limit": {"segment_type": "power_limit", "max_power": np.array([10.0, 10.0])},
        },
    }
    setters = _discover_setters(conn, model_config)
    paths = [path for path, _setter in setters]
    assert ("segments", "power_limit", "max_power") in paths


def test_discover_setters_skips_non_tracked_params() -> None:
    """Non-TrackedParam attributes like source/target are not bound."""
    network = _simple_network()
    conn = network.elements["conn"]
    model_config = {
        "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
        "name": "conn",
        "source": "source",
        "target": "target",
        "segments": {
            "power_limit": {"segment_type": "power_limit", "max_power": np.array([10.0, 10.0])},
        },
    }
    setters = _discover_setters(conn, model_config)
    paths = {path for path, _setter in setters}
    assert ("source",) not in paths
    assert ("target",) not in paths


# ---------------------------------------------------------------------------
# _build_element_updater
# ---------------------------------------------------------------------------


def test_element_updater_updates_tracked_params() -> None:
    """Element updater writes fresh values to TrackedParams."""
    network = _simple_network()
    conn = network.elements["conn"]
    assert isinstance(conn, Connection)
    pl = conn.segments["power_limit"]
    assert isinstance(pl, PowerLimitSegment)
    assert pl.max_power is not None
    assert pl.max_power[0] == 10.0

    initial_model_configs: list[ModelElementConfig] = [
        ConnectionElementConfig(
            element_type=MODEL_ELEMENT_TYPE_CONNECTION,
            name="conn",
            source="source",
            target="target",
            tags={1},
            segments={
                "power_limit": {"segment_type": "power_limit", "max_power": np.array([10.0, 10.0])},
            },
        )
    ]
    updater = _build_element_updater(network, ElementType.CONNECTION, initial_model_configs)

    fresh_config = _connection_config(max_power=np.array([20.0, 20.0]))
    updater(fresh_config)

    assert pl.max_power[0] == 20.0


def test_element_updater_handles_none_efficiency() -> None:
    """Clearing optional efficiency via updater sets TrackedParam to None."""
    network = Network(name="test", periods=np.array([1.0, 1.0]))
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "source", "is_source": True, "is_sink": False})
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "target", "is_source": False, "is_sink": True})
    network.add(
        {
            "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
            "name": "conn",
            "source": "source",
            "target": "target",
            "tags": {1},
            "segments": {
                "efficiency": {"segment_type": "efficiency", "efficiency": np.array([0.95, 0.95])},
                "power_limit": {"segment_type": "power_limit", "max_power": np.array([10.0, 10.0])},
                "pricing": {"segment_type": "pricing", "price": np.array([0.10, 0.10])},
            },
        }
    )

    initial_model_configs: list[ModelElementConfig] = [
        ConnectionElementConfig(
            element_type=MODEL_ELEMENT_TYPE_CONNECTION,
            name="conn",
            source="source",
            target="target",
            tags={1},
            segments={
                "efficiency": {"segment_type": "efficiency", "efficiency": np.array([0.95, 0.95])},
                "power_limit": {"segment_type": "power_limit", "max_power": np.array([10.0, 10.0])},
                "pricing": {"segment_type": "pricing", "price": np.array([0.10, 0.10])},
            },
        )
    ]
    updater = _build_element_updater(network, ElementType.CONNECTION, initial_model_configs)

    fresh_config = _connection_config(
        max_power=np.array([10.0, 10.0]),
        price=np.array([0.10, 0.10]),
    )
    updater(fresh_config)

    conn = network.elements["conn"]
    assert isinstance(conn, Connection)
    efficiency = conn.segments["efficiency"]
    assert isinstance(efficiency, EfficiencySegment)
    assert efficiency.efficiency is None

    network.optimize()


# ---------------------------------------------------------------------------
# _build_policy_updater
# ---------------------------------------------------------------------------


def _policy_network() -> Network:
    """Create a network with a tagged connection and PolicyPricing element."""
    network = Network(name="test", periods=np.array([1.0]))
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "src", "is_source": True, "is_sink": False})
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "dst", "is_source": False, "is_sink": True})
    network.add(
        {
            "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
            "name": "line",
            "source": "src",
            "target": "dst",
            "tags": {0, 1},
            "segments": {
                "power_limit": {
                    "segment_type": "power_limit",
                    "max_power": np.array([10.0]),
                    "max_power_source_target": np.array([10.0]),
                    "max_power_target_source": np.array([10.0]),
                },
            },
        }
    )
    network.add(
        PolicyPricingElementConfig(
            element_type=MODEL_ELEMENT_TYPE_POLICY_PRICING,
            name="policy_pricing_r0_v1",
            price=0.05,
            terms=[PolicyPricingTerm(connection="line", tag=1)],
        )
    )
    return network


def test_policy_updater_updates_price() -> None:
    """Policy updater writes fresh price to PolicyPricing elements."""
    network = _policy_network()
    updater = _build_policy_updater(network, {0: ["policy_pricing_r0_v1"]})

    config: ElementConfigData = {
        CONF_ELEMENT_TYPE: ElementType.POLICY,
        CONF_NAME: "Policies",
        "rules": [{"name": "rule", "enabled": True, "source": ["src"], "target": ["dst"], "price": 0.10}],
    }
    updater(config)

    elem = network.elements["policy_pricing_r0_v1"]
    assert isinstance(elem, PolicyPricing)
    assert elem.price == pytest.approx([0.10])


def test_policy_updater_zeros_disabled_rule() -> None:
    """Policy updater writes zero price for disabled rules."""
    network = _policy_network()
    updater = _build_policy_updater(network, {0: ["policy_pricing_r0_v1"]})

    config: ElementConfigData = {
        CONF_ELEMENT_TYPE: ElementType.POLICY,
        CONF_NAME: "Policies",
        "rules": [{"name": "rule", "enabled": False, "source": ["src"], "target": ["dst"], "price": 0.10}],
    }
    updater(config)

    elem = network.elements["policy_pricing_r0_v1"]
    assert isinstance(elem, PolicyPricing)
    assert elem.price == pytest.approx([0.0])


def test_policy_updater_zeros_stale_rules() -> None:
    """Policy updater zeros pricing when rule index exceeds rule count."""
    network = _policy_network()
    updater = _build_policy_updater(network, {0: ["policy_pricing_r0_v1"]})

    config: ElementConfigData = {
        CONF_ELEMENT_TYPE: ElementType.POLICY,
        CONF_NAME: "Policies",
        "rules": [],
    }
    updater(config)

    elem = network.elements["policy_pricing_r0_v1"]
    assert isinstance(elem, PolicyPricing)
    assert elem.price == pytest.approx([0.0])


def test_policy_updater_reenables_rule() -> None:
    """Policy updater restores price when re-enabling a rule."""
    network = _policy_network()
    updater = _build_policy_updater(network, {0: ["policy_pricing_r0_v1"]})

    # Disable
    updater(
        {
            CONF_ELEMENT_TYPE: ElementType.POLICY,
            CONF_NAME: "Policies",
            "rules": [{"name": "rule", "enabled": False, "source": ["src"], "target": ["dst"], "price": 0.07}],
        }
    )
    elem = network.elements["policy_pricing_r0_v1"]
    assert isinstance(elem, PolicyPricing)
    assert elem.price == pytest.approx([0.0])

    # Re-enable
    updater(
        {
            CONF_ELEMENT_TYPE: ElementType.POLICY,
            CONF_NAME: "Policies",
            "rules": [{"name": "rule", "enabled": True, "source": ["src"], "target": ["dst"], "price": 0.07}],
        }
    )
    assert elem.price == pytest.approx([0.07])


# ---------------------------------------------------------------------------
# soc_pricing TrackedParam discovery (issue #467)
# ---------------------------------------------------------------------------


def _battery_discharge_network() -> Network:
    """Build a network with a Battery and a discharge connection that has soc_pricing."""
    network = Network(name="test", periods=np.array([1.0, 1.0]))
    network.add(
        {
            "element_type": "battery",
            "name": "Battery",
            "capacity": np.array([10.0, 10.0, 10.0]),
            "initial_charge": 5.0,
            "salvage_value": 0.0,
        }
    )
    network.add({"element_type": MODEL_ELEMENT_TYPE_NODE, "name": "Grid", "is_source": False, "is_sink": True})
    network.add(
        {
            "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
            "name": "Battery:discharge",
            "source": "Battery",
            "target": "Grid",
            "tags": {1},
            "segments": {
                "soc_pricing": {
                    "segment_type": "soc_pricing",
                    "discharge_energy_threshold": np.array([2.0, 2.0]),
                    "discharge_energy_price": np.array([0.10, 0.10]),
                },
            },
        }
    )
    return network


def test_discover_setters_finds_soc_pricing_thresholds() -> None:
    """_discover_setters captures soc_pricing threshold/price as TrackedParams.

    Regression for issue #467: previously these were instance-only attributes
    so the updater could not refresh them when min_charge_percentage changed
    on the battery participant.
    """
    network = _battery_discharge_network()
    conn = network.elements["Battery:discharge"]
    model_config = {
        "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
        "name": "Battery:discharge",
        "source": "Battery",
        "target": "Grid",
        "tags": {1},
        "segments": {
            "soc_pricing": {
                "segment_type": "soc_pricing",
                "discharge_energy_threshold": np.array([2.0, 2.0]),
                "discharge_energy_price": np.array([0.10, 0.10]),
            },
        },
    }
    setters = _discover_setters(conn, model_config)
    paths = {path for path, _setter in setters}
    assert ("segments", "soc_pricing", "discharge_energy_threshold") in paths
    assert ("segments", "soc_pricing", "discharge_energy_price") in paths


def test_soc_pricing_setters_write_fresh_threshold() -> None:
    """Setters captured for soc_pricing write fresh values into the segment.

    This is the regression for issue #467: when the user edits the battery
    min_charge_percentage, the adapter recomputes discharge_energy_threshold
    and the captured setter must propagate that to the live SocPricingSegment
    so the LP sees the new floor.
    """
    network = _battery_discharge_network()
    conn = network.elements["Battery:discharge"]
    assert isinstance(conn, Connection)
    seg = conn.segments["soc_pricing"]
    assert isinstance(seg, SocPricingSegment)
    assert seg.discharge_energy_threshold is not None
    assert seg.discharge_energy_threshold[0] == pytest.approx(2.0)

    model_config = {
        "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
        "name": "Battery:discharge",
        "source": "Battery",
        "target": "Grid",
        "tags": {1},
        "segments": {
            "soc_pricing": {
                "segment_type": "soc_pricing",
                "discharge_energy_threshold": np.array([2.0, 2.0]),
                "discharge_energy_price": np.array([0.10, 0.10]),
            },
        },
    }

    setters = _discover_setters(conn, model_config)
    setter_by_path = dict(setters)
    threshold_setter = setter_by_path[("segments", "soc_pricing", "discharge_energy_threshold")]
    threshold_setter(np.array([4.0, 4.0]))

    assert seg.discharge_energy_threshold is not None
    assert seg.discharge_energy_threshold[0] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# _collect_policy_rules
# ---------------------------------------------------------------------------


def test_collect_policy_rules_merges_multiple_policy_participants() -> None:
    """Multiple policy participants are merged into one compiled rules list."""
    participants: dict[str, Any] = {
        "Policies A": {
            CONF_ELEMENT_TYPE: ElementType.POLICY,
            CONF_NAME: "Policies",
            "rules": [
                {"name": "A", "enabled": True, "source": ["Solar"], "price": {"type": "constant", "value": 0.01}}
            ],
        },
        "Policies B": {
            CONF_ELEMENT_TYPE: ElementType.POLICY,
            CONF_NAME: "Policies",
            "rules": [{"name": "B", "enabled": True, "target": ["Load"], "price": {"type": "constant", "value": 0.02}}],
        },
    }

    rules = _collect_policy_rules(participants)
    assert len(rules) == 2


@pytest.fixture
def inverter_participants() -> dict[str, ElementConfigData]:
    """Create fixed AC/DC demands, asymmetric losses and no battery or reserve."""
    return {
        "AC": {"element_type": ElementType.NODE, "name": "AC", "role": {}},
        "Grid": {
            "element_type": ElementType.GRID,
            "name": "Grid",
            "connection": as_connection_target("AC"),
            "power_limits": {"max_power_source_target": 10.0, "max_power_target_source": 0.0},
            "pricing": {"price_source_target": np.array([-0.1, -0.3, 0.15]), "price_target_source": 0.0},
        },
        "Inverter": {
            "element_type": ElementType.INVERTER,
            "name": "Inverter",
            "connection": as_connection_target("AC"),
            "power_limits": {"max_power_source_target": 10.0, "max_power_target_source": 10.0},
            "efficiency": {"efficiency_source_target": 0.83, "efficiency_target_source": 0.91},
        },
        **{
            name: {
                "element_type": ElementType.LOAD,
                "name": name,
                "connection": as_connection_target(target),
                "forecast": {"forecast": power},
                "curtailment": {"curtailment": False},
            }
            for name, target, power in (("Load", "AC", 0.9), ("DC load", "Inverter", 1.0))
        },
    }


@pytest.mark.parametrize("options", [LexOptions(), BlendedOptions(), CalibratedOptions()])
@pytest.mark.parametrize("limit", [10.0, None])
async def test_inverter_negative_price_does_not_create_demand(
    hub_entry: MockConfigEntry,
    inverter_participants: dict[str, ElementConfigData],
    options: SolveOptions,
    limit: float | None,
) -> None:
    """Native cold/warm solves serve fixed loads without profitable converter loops.

    Cover all objective modes, optional nameplate limits, asymmetric losses and
    variable durations. Energy and cost follow household demand, not invented
    dissipation; there is no storage or reserve policy in this regression.
    """
    inverter = inverter_participants["Inverter"]
    assert inverter["element_type"] == ElementType.INVERTER
    inverter["power_limits"] = (
        {} if limit is None else {"max_power_source_target": limit, "max_power_target_source": limit}
    )
    network, _ = await create_network(hub_entry, periods_seconds=[900, 3600, 1800], participants=inverter_participants)
    network.options = options
    for _ in range(2):
        cost = network.optimize()
        grid = expect_output_data(network.elements["Grid:import"].outputs()["connection_power"])
        forward = expect_output_data(network.elements["Inverter:dc_to_ac"].outputs()["connection_power"])
        reverse = expect_output_data(network.elements["Inverter:ac_to_dc"].outputs()["connection_power"])
        np.testing.assert_allclose(grid.values, [0.9 + 1.0 / 0.91] * 3, atol=1e-6)
        np.testing.assert_allclose(forward.values, [0.0] * 3, atol=1e-6)
        np.testing.assert_allclose(reverse.values, [1.0 / 0.91] * 3, atol=1e-6)
        expected_cost = (0.9 + 1.0 / 0.91) * (-0.1 * 0.25 - 0.3 + 0.15 * 0.5)
        assert cost == pytest.approx(expected_cost, abs=1e-6)


async def test_inverter_replans_directions_and_limits(
    hub_entry: MockConfigEntry, inverter_participants: dict[str, ElementConfigData]
) -> None:
    """Warm input updates release previous choices and refresh directional limits.

    Negative imports curtail solar; positive imports use it, respecting updated
    post-efficiency caps. Increasing a cap and reversing prices must also work on
    the same reusable network with changed period lengths.
    """
    participants = inverter_participants
    participants["Solar"] = {
        "element_type": ElementType.SOLAR,
        "name": "Solar",
        "connection": as_connection_target("Inverter"),
        "forecast": {"forecast": 2.0},
        "curtailment": {"curtailment": True},
    }
    grid_config = participants["Grid"]
    inverter = participants["Inverter"]
    assert grid_config["element_type"] == ElementType.GRID
    assert inverter["element_type"] == ElementType.INVERTER
    grid_config["pricing"]["price_source_target"] = -0.3
    network, updaters = await create_network(hub_entry, periods_seconds=[900, 3600, 1800], participants=participants)
    network.optimize()
    grid = expect_output_data(network.elements["Grid:import"].outputs()["connection_power"])
    np.testing.assert_allclose(grid.values, [0.9 + 1.0 / 0.91] * 3, atol=1e-6)

    grid_config["pricing"]["price_source_target"] = 0.3
    caps = np.array([0.4, 0.6, 0.2])
    inverter["power_limits"]["max_power_source_target"] = caps
    updaters["Grid"](grid_config)
    updaters["Inverter"](inverter)
    network.update_periods(np.array([0.2, 0.6, 1.1]))
    network.optimize()
    forward = expect_output_data(network.elements["Inverter:dc_to_ac"].outputs()["connection_power"])
    grid = expect_output_data(network.elements["Grid:import"].outputs()["connection_power"])
    np.testing.assert_allclose(forward.values, caps / 0.83, atol=1e-6)
    np.testing.assert_allclose(grid.values, 0.9 - caps, atol=1e-6)

    inverter["power_limits"]["max_power_source_target"] = 10.0
    updaters["Inverter"](inverter)
    network.optimize()
    grid = expect_output_data(network.elements["Grid:import"].outputs()["connection_power"])
    np.testing.assert_allclose(grid.values, [0.9 - 0.83] * 3, atol=1e-6)

    grid_config["pricing"]["price_source_target"] = -0.3
    updaters["Grid"](grid_config)
    network.optimize()
    grid = expect_output_data(network.elements["Grid:import"].outputs()["connection_power"])
    np.testing.assert_allclose(grid.values, [0.9 + 1.0 / 0.91] * 3, atol=1e-6)


async def test_inverter_custom_objective_cost_certificate(
    hub_entry: MockConfigEntry, inverter_participants: dict[str, ElementConfigData]
) -> None:
    """Certify a feasible LP candidate against a horizon-wide economic bound.

    Small artificial loss profits fit the requested tolerance; larger profits
    require exact direction selection. Both return physical demand and honest
    gaps. Integer solutions with invalid duals must not publish zero prices.
    """
    grid_config = inverter_participants["Grid"]
    assert grid_config["element_type"] == ElementType.GRID
    grid_config["pricing"]["price_source_target"] = np.array([-1e-5, -3e-5, 1.5e-5])
    network, updaters = await create_network(
        hub_entry, periods_seconds=[900, 3600, 1800], participants=inverter_participants
    )
    costs = network.cost()
    assert costs is not None
    assert costs[0] is not None
    cost, gap = network.minimize(costs[0], absolute_tolerance=0.001)
    required_import = 0.9 + 1.0 / 0.91
    # Reverse output reaches 10 kW; 1 kW serves DC demand and 9 kW
    # circulates forward at 83%. Independently calculate the relaxed bound.
    relaxed_import = 0.9 + 10.0 / 0.91 - 9.0 * 0.83
    expected_gap = (relaxed_import - required_import) * (1e-5 * 0.25 + 3e-5)
    assert cost == pytest.approx(required_import * (-1e-5 * 0.25 - 3e-5 + 1.5e-5 * 0.5))
    assert gap == pytest.approx(expected_gap)
    assert 0 < gap < 0.001
    assert len(expect_output_data(network.elements["AC"].outputs()[ELEMENT_POWER_BALANCE]).values) == 3

    grid_config["pricing"]["price_source_target"] = np.array([-0.1, -0.3, 0.15])
    updaters["Grid"](grid_config)
    costs = network.cost()
    assert costs is not None
    assert costs[0] is not None
    cost, gap = network.minimize(costs[0], absolute_tolerance=0.001)
    assert cost == pytest.approx(required_import * (-0.1 * 0.25 - 0.3 + 0.15 * 0.5))
    assert gap == pytest.approx(0.0, abs=1e-8)
    grid = expect_output_data(network.elements["Grid:import"].outputs()["connection_power"])
    np.testing.assert_allclose(grid.values, [required_import] * 3, atol=1e-6)
    assert expect_output_data(network.elements["AC"].outputs()[ELEMENT_POWER_BALANCE]).values == ()

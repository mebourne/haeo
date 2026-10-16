"""Configured reserve policy contracts through native forecast outputs."""

from collections.abc import Sequence

import numpy as np
import pytest

from custom_components.haeo.core.adapters.registry import ELEMENT_TYPES
from custom_components.haeo.core.adapters.reserve import ReservePlanner, ReservePolicy, ReserveResult
from custom_components.haeo.core.model import OutputData
from custom_components.haeo.core.schema import as_connection_target
from custom_components.haeo.core.schema.elements import ElementConfigData, ElementType


def reserve_participants(
    *,
    soc: float = 0.30,
    prices: Sequence[float] = (21, 0.15, 0.15, 0.15, 0.15, 0.15, 0.15),
    imports: Sequence[float] = (4, 4, 4, 4, 4, 0.09, 0.09),
    solar: Sequence[float] = (0, 0, 0, 0, 0, 0, 0),
    capacity: float = 40.3,
    battery_efficiency: float = 0.95,
    inverter_efficiency: float = 0.95,
) -> dict[str, ElementConfigData]:
    """Build a configured AC/DC network with deliberately non-default names."""
    n = len(prices)
    return {
        "AC bus": {"element_type": ElementType.NODE, "name": "AC bus", "role": {}},
        "Storage": {
            "element_type": ElementType.BATTERY,
            "name": "Storage",
            "connection": as_connection_target("Converter"),
            "storage": {"capacity": np.full(n + 1, capacity), "initial_charge_percentage": soc},
            "limits": {"min_charge_percentage": 0.30, "max_charge_percentage": 0.95},
            "undercharge": {"percentage": 0.10, "cost": 4.0},
            "overcharge": {"percentage": 1.0, "cost": 0.02},
            "partitioning": {},
            "power_limits": {"max_power_source_target": 24.0, "max_power_target_source": 21.0},
            "efficiency": {
                "efficiency_source_target": battery_efficiency,
                "efficiency_target_source": 0.91,
            },
            "pricing": {"salvage_value": 0.5},
        },
        "Market": {
            "element_type": ElementType.GRID,
            "name": "Market",
            "connection": as_connection_target("AC bus"),
            "power_limits": {"max_power_source_target": 10.0, "max_power_target_source": 10.0},
            "pricing": {"price_source_target": np.asarray(imports), "price_target_source": np.asarray(prices)},
        },
        "Converter": {
            "element_type": ElementType.INVERTER,
            "name": "Converter",
            "connection": as_connection_target("AC bus"),
            "power_limits": {"max_power_source_target": 9.999, "max_power_target_source": 9.999},
            "efficiency": {
                "efficiency_source_target": inverter_efficiency,
                "efficiency_target_source": 0.93,
            },
        },
        "PV": {
            "element_type": ElementType.SOLAR,
            "name": "PV",
            "connection": as_connection_target("Converter"),
            "forecast": {"forecast": np.asarray(solar)},
            "curtailment": {"curtailment": True},
        },
        "House": {
            "element_type": ElementType.LOAD,
            "name": "House",
            "connection": as_connection_target("AC bus"),
            "forecast": {"forecast": np.full(n, 0.6)},
            "curtailment": {"curtailment": False},
        },
        "Idle": {
            "element_type": ElementType.LOAD,
            "name": "Idle",
            "connection": as_connection_target("Converter"),
            "forecast": {"forecast": np.full(n, 0.165)},
            "curtailment": {"curtailment": False},
        },
        "Valuations": {
            "element_type": ElementType.POLICY,
            "name": "Valuations",
            "rules": [
                {"name": "Wear", "enabled": True, "source": ["Storage"], "target": ["Market"], "price": 0.08},
                {"name": "Charge", "enabled": True, "target": ["Storage"], "price": -0.001},
            ],
        },
    }


def reserve_outputs(
    result: ReserveResult, participants: dict[str, ElementConfigData]
) -> dict[str, dict[str, OutputData]]:
    """Read native public adapter outputs, as the coordinator does."""
    model_outputs = {name: element.outputs() for name, element in result.network.elements.items()}
    outputs: dict[str, dict[str, OutputData]] = {}
    for name, config in participants.items():
        devices = ELEMENT_TYPES[config["element_type"]].outputs(
            name=name, model_outputs=model_outputs, config=config, periods=result.network.periods
        )
        outputs[name] = {key: value for device in devices.values() for key, value in device.items()}
    return outputs


@pytest.mark.parametrize(
    ("capacity", "battery_efficiency", "inverter_efficiency"), [(40.3, 0.95, 0.95), (31, 0.8, 0.9)]
)
def test_current_spike_prices_local_supply_without_expensive_reserve_buyback(
    capacity: float, battery_efficiency: float, inverter_efficiency: float
) -> None:
    """Use configured ratings/losses and publish the familiar power and SOC forecasts."""
    participants = reserve_participants(
        capacity=capacity, battery_efficiency=battery_efficiency, inverter_efficiency=inverter_efficiency
    )
    result = ReservePlanner(participants, np.ones(7), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    expected_export = 0.20 * capacity * battery_efficiency * inverter_efficiency - 0.6 - 0.165 * inverter_efficiency
    assert outputs["Market"]["grid_power_export"].values[0] == pytest.approx(expected_export, abs=1e-4)
    assert outputs["Market"]["grid_power_import"].values[1] == pytest.approx(0.6 + 0.165 / 0.93, abs=1e-4)
    assert max(outputs["Storage"]["battery_power_charge"].values[:5]) < 1e-5
    assert outputs["Market"]["grid_power_import"].values[5] > 0
    assert outputs["Storage"]["battery_state_of_charge"].values[5] == pytest.approx(0.10, abs=1e-5)
    assert outputs["Storage"]["battery_power_balance"].values == ()
    assert outputs["Converter"]["inverter_dc_bus_power_balance"].values == ()
    battery = participants["Storage"]
    assert battery["element_type"] == ElementType.BATTERY
    assert battery.get("undercharge") == {"percentage": 0.10, "cost": 4.0}
    assert battery.get("overcharge") == {"percentage": 1.0, "cost": 0.02}


@pytest.mark.parametrize(
    ("soc", "prices", "hours", "expected_now", "expected_later"),
    [
        (0.725, [8, 10, 0.15, 0.15, 0.15, 0.15, 0.15], [1] * 7, 9.399, 9.399),
        (0.60, [4, 20, 0.15, 0.15, 0.15, 0.15, 0.15], [1, 2, 1, 1, 1, 1, 1], 0, 6.8224375),
    ],
    ids=["capture_both", "save_for_twenty"],
)
def test_qualifying_events_share_the_safe_energy_budget(
    soc: float, prices: list[float], hours: list[int], expected_now: float, expected_later: float
) -> None:
    """Capture both spikes when possible and favor higher profit under true scarcity."""
    participants = reserve_participants(soc=soc, prices=prices, imports=[20] * 5 + [0.09, 0.09])
    result = ReservePlanner(participants, np.asarray(hours, dtype=float), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    assert outputs["Market"]["grid_power_export"].values[:2] == pytest.approx([expected_now, expected_later], abs=1e-4)
    assert min(outputs["Storage"]["battery_state_of_charge"].values) >= 0.10 - 1e-5


@pytest.mark.parametrize("soc", [0.21, 0.30 - 0.0000001, 0.30, 0.30 + 0.0000001])
def test_ordinary_sales_remain_protected_at_the_preferred_boundary(soc: float) -> None:
    """Local supply can use reserve while ordinary exports cannot spend it."""
    participants = reserve_participants(soc=soc, prices=[0.15] * 7)
    result = ReservePlanner(participants, np.ones(7), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    assert max(outputs["Market"]["grid_power_export"].values) < 1e-5
    assert max(outputs["Storage"]["battery_power_charge"].values[:5]) < 1e-5
    assert min(outputs["Storage"]["battery_state_of_charge"].values) >= 0.10 - 1e-5


def test_asymmetric_periods_protect_the_physical_prefix_before_refill() -> None:
    """Later replenishment cannot cover an earlier hard-floor breach."""
    participants = reserve_participants()
    periods = np.array([0.25, 0.5, 1.5, 0.25, 2, 1, 1])
    result = ReservePlanner(participants, periods, ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    assert outputs["Market"]["grid_power_export"].values[0] == pytest.approx(9.399, abs=1e-4)
    early_discharge = ((9.999 / 0.95 + 0.165) / 0.95) * 0.25
    later_discharge = (0.6 / 0.9025 + 0.165 / 0.95) * sum(periods[1:5])
    expected_soc = (40.3 * 0.30 - early_discharge - later_discharge) / 40.3
    assert min(outputs["Storage"]["battery_state_of_charge"].values) == pytest.approx(expected_soc, abs=1e-5)
    assert max(outputs["Storage"]["battery_power_charge"].values[:5]) < 1e-5


def test_solar_saturation_does_not_warrant_battery_export() -> None:
    """Battery export must add value rather than merely displace inverter-limited solar."""
    participants = reserve_participants(soc=0.30, solar=[14] * 7)
    result = ReservePlanner(participants, np.ones(7), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    assert outputs["Market"]["grid_power_export"].values[0] == pytest.approx(9.399, abs=1e-4)
    assert outputs["Storage"]["battery_power_discharge"].values[0] < 1e-5


def test_ordinary_sale_cannot_manufacture_reserve_use_for_a_later_spike() -> None:
    """The normal budget must cover local use and the valuable forecast first."""
    participants = reserve_participants(soc=0.80, prices=[2, 8] + [0.15] * 5)
    result = ReservePlanner(participants, np.ones(7), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    local_use = 5 * (0.6 / 0.9025 + 0.165 / 0.95)
    expected_now = ((0.80 - 0.30) * 40.3 - local_use) * 0.9025 - 9.399
    assert outputs["Market"]["grid_power_export"].values[:2] == pytest.approx([expected_now, 9.399], abs=1e-4)
    assert min(outputs["Storage"]["battery_state_of_charge"].values) >= 0.30 - 1e-5


@pytest.mark.parametrize("house_kw", [0.6, 2.0])
def test_cold_low_soc_can_buy_for_household_use_without_forced_battery_refill(house_kw: float) -> None:
    """Insufficient inventory must permit grid support, not an expensive reserve debt."""
    participants = reserve_participants(soc=0.20, prices=[0.15] * 7)
    house = participants["House"]
    assert house["element_type"] == ElementType.LOAD
    house["forecast"]["forecast"] = np.full(7, house_kw)
    result = ReservePlanner(participants, np.ones(7), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    assert max(outputs["Market"]["grid_power_export"].values) < 1e-5
    assert sum(outputs["Market"]["grid_power_import"].values[:5]) > 0
    assert max(outputs["Storage"]["battery_power_charge"].values[:5]) < 1e-5
    assert min(outputs["Storage"]["battery_state_of_charge"].values) >= 0.10 - 1e-5


@pytest.mark.parametrize("imports", [[22, 10], [22, 10, 0.10]])
def test_twenty_dollar_sale_can_replace_ten_dollar_household_supply(imports: list[float]) -> None:
    """Sell valuable energy rather than reserving it for less valuable household use."""
    n = len(imports)
    participants = reserve_participants(prices=[20] + [0.15] * (n - 1), imports=imports)
    result = ReservePlanner(participants, np.ones(n), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    # 8.06 stored kWh above the floor, less current AC and DC demand and losses.
    expected_export = 40.3 * 0.20 * 0.95 * 0.95 - 0.6 - 0.165 * 0.95
    assert outputs["Market"]["grid_power_export"].values[0] == pytest.approx(expected_export, abs=1e-4)
    assert outputs["Storage"]["battery_state_of_charge"].values[1] == pytest.approx(0.10, abs=1e-5)
    # Supply later loads from the grid, but do not restore stock at $10/kWh.
    assert outputs["Market"]["grid_power_import"].values[1] == pytest.approx(0.6 + 0.165 / 0.93, abs=1e-4)
    assert outputs["Storage"]["battery_power_charge"].values[1] < 1e-5
    if n == 3:
        assert outputs["Storage"]["battery_power_charge"].values[2] > 8


@pytest.mark.parametrize(
    "imports",
    [[0.60, 2.20], [0.60, 2.20, 4], [0.60, 2.20, 4, 0.09]],
    ids=["same_period_load", "future_load", "later_refill"],
)
def test_ordinary_trading_can_reoptimize_charging_above_the_old_import_cutoff(imports: list[float]) -> None:
    """Normal arbitrage must cover every household-use prefix, not just the sale period."""
    n = len(imports)
    participants = reserve_participants(prices=[0.15, 2] + [0.15] * (n - 2), imports=imports, solar=[0] * n)
    result = ReservePlanner(participants, np.ones(n), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    expected_charge = (10 - 0.6) * 0.93 - 0.165
    protected_hours = 1 if n == 2 else 2
    expected_export = expected_charge * 0.91 * 0.95 * 0.95 - protected_hours * (0.6 + 0.165 * 0.95)
    assert outputs["Market"]["grid_power_import"].values[0] == pytest.approx(10, abs=1e-4)
    assert outputs["Storage"]["battery_power_charge"].values[0] == pytest.approx(expected_charge, abs=1e-4)
    assert outputs["Market"]["grid_power_export"].values[1] == pytest.approx(expected_export, abs=1e-4)
    assert min(outputs["Storage"]["battery_state_of_charge"].values) >= 0.30 - 1e-5


def test_reserve_does_not_restrict_economic_household_use() -> None:
    """Preferred stock protects sales, not household supply or a recharge target."""
    participants = reserve_participants(soc=0.305, prices=[0.15] * 6, imports=[4] * 6, solar=[0] * 6)
    result = ReservePlanner(participants, np.ones(6), ReservePolicy()).solve()
    outputs = reserve_outputs(result, participants)
    expected_soc = 0.305 - 6 * (0.6 / 0.9025 + 0.165 / 0.95) / 40.3
    assert min(outputs["Storage"]["battery_state_of_charge"].values) == pytest.approx(expected_soc, abs=1e-5)
    assert max(outputs["Market"]["grid_power_import"].values) < 1e-5
    assert max(outputs["Market"]["grid_power_export"].values) < 1e-5
    assert max(outputs["Storage"]["battery_power_charge"].values) < 1e-5


@pytest.mark.parametrize(("price", "expected_export"), [(3.99, 0), (4, 6.5174)])
def test_export_cutoff_changes_sale_permission_not_household_use(price: float, expected_export: float) -> None:
    """The cutoff is inclusive; rejecting a sale does not impose a household floor."""
    participants = reserve_participants(prices=[price], imports=[22], solar=[0])
    outputs = reserve_outputs(ReservePlanner(participants, np.ones(1), ReservePolicy()).solve(), participants)
    assert outputs["Market"]["grid_power_export"].values[0] == pytest.approx(expected_export, abs=1e-4)
    assert outputs["Storage"]["battery_state_of_charge"].values[1] < 0.30


def test_ordinary_sale_is_omitted_when_a_later_qualifying_event_uses_reserve() -> None:
    """An ordinary sale must not precede a below-preferred forecast, even with additional charging."""
    participants = reserve_participants(soc=0.65, prices=[0.15, 2, 8], imports=[0.60, 2.20, 20], solar=[0] * 3)
    outputs = reserve_outputs(ReservePlanner(participants, np.array([1, 1, 2]), ReservePolicy()).solve(), participants)
    assert outputs["Market"]["grid_power_export"].values[1:] == pytest.approx([0, 9.399], abs=1e-4)
    assert min(outputs["Storage"]["battery_state_of_charge"].values) == pytest.approx(0.10, abs=1e-5)


def test_solar_export_below_preferred_level_does_not_require_refill_or_extra_battery_use() -> None:
    """Ordinary solar may export its surplus, but cannot borrow reserve to free more solar."""
    participants = reserve_participants(soc=0.20, prices=[2], imports=[4], solar=[2])
    outputs = reserve_outputs(ReservePlanner(participants, np.ones(1), ReservePolicy()).solve(), participants)
    expected_solar_export = (2 - 0.165) * 0.95 - 0.6
    assert outputs["Market"]["grid_power_export"].values[0] == pytest.approx(expected_solar_export, abs=1e-4)
    assert outputs["Storage"]["battery_power_discharge"].values[0] < 1e-5
    assert outputs["Storage"]["battery_power_charge"].values[0] < 1e-5

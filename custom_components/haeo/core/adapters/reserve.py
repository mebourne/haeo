"""Price-qualified reserve planning on an isolated, configured energy network."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from highspy import Highs, HighsModelStatus, HighsVarType
from highspy.highs import HighspyArray, highs_linear_expression
import numpy as np
from numpy.typing import NDArray

from custom_components.haeo.core.adapters.elements.policy import extract_policy_rules
from custom_components.haeo.core.adapters.policy_compilation import compile_policies
from custom_components.haeo.core.adapters.registry import collect_model_elements
from custom_components.haeo.core.const import DEFAULT_RESERVE_EXPORT_PRICE
from custom_components.haeo.core.model.elements.battery import Battery
from custom_components.haeo.core.model.elements.connection import Connection, ConnectionOutputName
from custom_components.haeo.core.model.network import Network
from custom_components.haeo.core.model.util import broadcast_to_sequence
from custom_components.haeo.core.schema import extract_connection_target
from custom_components.haeo.core.schema.elements import ElementConfigData, ElementType
from custom_components.haeo.core.schema.elements.battery import BatteryConfigData
from custom_components.haeo.core.schema.elements.grid import GridConfigData
from custom_components.haeo.core.schema.elements.inverter import InverterConfigData

# Energy tolerances are independent of the economic stopping tolerance.
ENERGY_TOLERANCE = 1e-6
OBJECTIVE_TOLERANCE = 0.001


class ReservePlanningError(ValueError):
    """No fresh, feasible reserve plan can be published."""


@dataclass(frozen=True)
class ReservePolicy:
    """Export-price eligibility for using stock below the preferred minimum."""

    export_price: float = DEFAULT_RESERVE_EXPORT_PRICE


@dataclass(frozen=True)
class ReserveResult:
    """Native solved network and certified economic stopping gap."""

    network: Network
    cost: float
    objective_gap: float


@dataclass
class _Model:
    network: Network
    solver: Highs
    battery: Battery
    connections: dict[str, Connection[ConnectionOutputName]]
    exports: HighspyArray
    battery_exports: HighspyArray


class ReservePlanner:
    """Plan one DC battery and grid with fixed loads on their AC/DC buses.

    Models are disposable and owned by one solve. Native reactive rows are never
    removed, and the integration's reusable LP is never modified by this policy.
    """

    def __init__(
        self,
        participants: Mapping[str, ElementConfigData],
        periods: NDArray[np.float64],
        policy: ReservePolicy,
    ) -> None:
        """Validate the supported topology and derive all limits from configuration."""
        self.participants = dict(participants)
        self.periods = periods
        batteries = [c for c in participants.values() if c["element_type"] == ElementType.BATTERY]
        grids = [c for c in participants.values() if c["element_type"] == ElementType.GRID]
        inverters = [c for c in participants.values() if c["element_type"] == ElementType.INVERTER]
        if len(batteries) != 1 or len(grids) != 1 or len(inverters) != 1:
            msg = "Reserve policy requires exactly one battery, one grid and one inverter"
            raise ReservePlanningError(msg)
        self.battery_config: BatteryConfigData = batteries[0]
        self.grid_config: GridConfigData = grids[0]
        self.inverter_config: InverterConfigData = inverters[0]
        self.battery_name = self.battery_config["name"]
        self.grid_name = self.grid_config["name"]
        self.inverter_name = self.inverter_config["name"]
        self.ac_bus = extract_connection_target(self.grid_config["connection"])
        if (
            extract_connection_target(self.inverter_config["connection"]) != self.ac_bus
            or extract_connection_target(self.battery_config["connection"]) != self.inverter_name
        ):
            msg = "Connect the reserve battery to the inverter and the inverter and grid to the same AC bus"
            raise ReservePlanningError(msg)
        ac_load = np.zeros(len(periods))
        dc_load = np.zeros(len(periods))
        solar = np.zeros(len(periods))
        for config in participants.values():
            match config["element_type"]:
                case ElementType.BATTERY | ElementType.GRID | ElementType.INVERTER | ElementType.POLICY:
                    continue
                case ElementType.NODE:
                    if config["name"] == self.ac_bus and not any(config["role"].values()):
                        continue
                case ElementType.LOAD:
                    target = extract_connection_target(config["connection"])
                    if not config["curtailment"].get("curtailment", False):
                        if target == self.ac_bus:
                            ac_load += self._series(config["forecast"]["forecast"])
                            continue
                        if target == self.inverter_name:
                            dc_load += self._series(config["forecast"]["forecast"])
                            continue
                case ElementType.SOLAR:
                    if extract_connection_target(config["connection"]) == self.inverter_name:
                        solar += self._series(config["forecast"]["forecast"])
                        continue
                case _:
                    pass
            msg = f"Unsupported reserve participant '{config['name']}'; use fixed AC/DC loads and DC solar"
            raise ReservePlanningError(msg)
        undercharge = self.battery_config.get("undercharge", {})
        if "percentage" not in undercharge or "cost" not in undercharge:
            msg = "Configure the battery undercharge partition: preferred minimum in Limits, hard floor in Undercharge"
            raise ReservePlanningError(msg)
        capacity = self._constant(self.battery_config["storage"]["capacity"], "rated capacity")
        hard_floor = self._constant(undercharge["percentage"], "hard floor")
        minimum = self._constant(self.battery_config["limits"].get("min_charge_percentage", 0.0), "preferred minimum")
        overcharge = self.battery_config.get("overcharge", {})
        maximum = self._constant(
            overcharge["percentage"]
            if "percentage" in overcharge and "cost" in overcharge
            else self.battery_config["limits"].get("max_charge_percentage", 1.0),
            "hard ceiling",
        )
        if capacity <= 0 or not 0 <= hard_floor < minimum < maximum <= 1:
            msg = "Reserve policy requires hard floor < preferred minimum < hard ceiling and positive rated capacity"
            raise ReservePlanningError(msg)
        self.floor = capacity * hard_floor
        self.reserve = capacity * (minimum - hard_floor)
        self.usable = capacity * (maximum - hard_floor)
        self.initial = capacity * self.battery_config["storage"]["initial_charge_percentage"] - self.floor
        if self.initial < -ENERGY_TOLERANCE or self.initial > self.usable + ENERGY_TOLERANCE:
            msg = "Observed battery SOC is outside the configured hard floor and ceiling"
            raise ReservePlanningError(msg)
        self.qualifying = self._series(self.grid_config["pricing"]["price_target_source"]) >= policy.export_price
        self.pairs = (
            (f"{self.battery_name}:discharge", f"{self.battery_name}:charge", self.battery_config),
            (f"{self.inverter_name}:dc_to_ac", f"{self.inverter_name}:ac_to_dc", self.inverter_config),
            (f"{self.grid_name}:import", f"{self.grid_name}:export", self.grid_config),
        )
        self.limits = {
            name: self._limit(config, key)
            for forward, reverse, config in self.pairs
            for name, key in ((forward, "max_power_source_target"), (reverse, "max_power_target_source"))
        }
        inverter_efficiency = self._series(self.inverter_config["efficiency"].get("efficiency_source_target", 1.0))
        self.solar_only = np.maximum(
            0,
            np.minimum(
                self.limits[f"{self.grid_name}:export"],
                np.minimum(
                    self.limits[f"{self.inverter_name}:dc_to_ac"] - ac_load,
                    (solar - dc_load) * inverter_efficiency - ac_load,
                ),
            ),
        )

    def _series(self, value: float | NDArray[np.floating[Any]]) -> NDArray[np.float64]:
        return broadcast_to_sequence(value, len(self.periods))

    @staticmethod
    def _constant(value: float | NDArray[np.floating[Any]], label: str) -> float:
        values = np.atleast_1d(value)
        if not np.all(np.isfinite(values)) or not np.allclose(values, values[0], rtol=0, atol=ENERGY_TOLERANCE):
            msg = f"Reserve policy requires a constant {label} over the horizon"
            raise ReservePlanningError(msg)
        return float(values[0])

    def _limit(self, config: BatteryConfigData | GridConfigData | InverterConfigData, key: str) -> NDArray[np.float64]:
        value = config["power_limits"].get(key)
        if value is None:
            msg = f"Set both power limits on '{config['name']}' before enabling the reserve policy"
            raise ReservePlanningError(msg)
        values = self._series(value)
        if not np.all(np.isfinite(values) & (values >= 0)):
            msg = f"Power limits for '{config['name']}' must be finite and nonnegative"
            raise ReservePlanningError(msg)
        return values

    def _build(self) -> _Model:
        # Preserve the stored settings for opt-out/rollback; replace only the
        # hourly lower penalty in the disposable reserve model.
        configs = dict(self.participants)
        configs[self.battery_name] = {
            **self.battery_config,
            "undercharge": {**self.battery_config.get("undercharge", {}), "cost": 0.0},
        }
        rules = [
            rule
            for config in configs.values()
            if config["element_type"] == ElementType.POLICY
            for rule in extract_policy_rules(config)
        ]
        # Keep battery provenance separate even if the user has no wear policy.
        rules.append({"sources": [self.battery_name], "destinations": ["*"], "price": 0.0})
        compiled = compile_policies(collect_model_elements(configs), rules)
        network = Network(name="Battery reserve", periods=self.periods)
        for config in compiled["elements"]:
            network.add(config)
        battery = network.elements[self.battery_name]
        assert isinstance(battery, Battery)  # noqa: S101 (the adapter creates the validated battery model)
        connections = {name: e for name, e in network.elements.items() if isinstance(e, Connection)}
        discharge = connections[f"{self.battery_name}:discharge"]
        tag = next(iter(discharge.power_in))
        exports = connections[f"{self.grid_name}:export"]
        battery_exports = exports.power_in[tag]
        # Solver extensions are confined to this disposable model, never the
        # coordinator's reusable native LP.
        solver = network._solver  # noqa: SLF001 (reserve planning owns this isolated model's solve lifecycle)
        for element in network.elements.values():
            element.constraints()
        return _Model(network, solver, battery, connections, exports.total_power_in, battery_exports)

    @staticmethod
    def _primary(model: _Model) -> highs_linear_expression:
        costs = model.network.cost()
        if costs is None or costs[0] is None:
            msg = "Reserve network requires an economic objective"
            raise ReservePlanningError(msg)
        return costs[0]

    @staticmethod
    def _minimize(model: _Model, objective: highs_linear_expression) -> float:
        model.solver.minimize(objective)
        if model.solver.getModelStatus() != HighsModelStatus.kOptimal:
            status = model.solver.modelStatusToString(model.solver.getModelStatus())
            msg = f"Reserve optimization did not finish: {status}"
            raise ReservePlanningError(msg)
        return model.solver.getObjectiveValue()

    def solve(self) -> ReserveResult:
        """Optimize all economics with a conditional preferred floor after ordinary sales."""
        model = self._build()
        h = model.solver
        n = len(self.periods)
        energy = model.battery.stored_energy
        ordinary_sold = h.addVariables(n, lb=0, ub=1, type=HighsVarType.kInteger, out_array=True)
        for t in range(n):
            # A positive ordinary battery sale protects every later boundary.
            # With no such sale the markers can stay zero: household use and
            # qualifying trades are free to draw down to the native hard floor.
            if t:
                h.addConstr(ordinary_sold[t] >= ordinary_sold[t - 1])
            if not self.qualifying[t]:
                h.addConstr(model.battery_exports[t] <= self.limits[f"{self.grid_name}:export"][t] * ordinary_sold[t])
                # Battery-supplied local use can free solar for export. Count
                # that incremental sale too, independent of source-tag routing.
                h.addConstr(
                    model.exports[t]
                    <= self.solar_only[t]
                    + (self.limits[f"{self.grid_name}:export"][t] - self.solar_only[t]) * ordinary_sold[t],
                )
            h.addConstr(energy[t + 1] >= self.reserve * ordinary_sold[t])
            for forward, reverse, _config in self.pairs:
                direction = h.addVariable(lb=0, ub=1, type=HighsVarType.kInteger)
                # Release limits apply at the power-limit segment, after losses,
                # not necessarily at the connection's reported input power.
                h.addConstr(
                    model.connections[forward].segments["power_limit"].total_power_in[t]
                    <= self.limits[forward][t] * direction
                )
                h.addConstr(
                    model.connections[reverse].segments["power_limit"].total_power_in[t]
                    <= self.limits[reverse][t] * (1 - direction)
                )
        h.setOptionValue("solver", "choose")
        h.setOptionValue("mip_rel_gap", 0.0)
        h.setOptionValue("mip_abs_gap", OBJECTIVE_TOLERANCE)
        primary = self._primary(model)
        cost = self._minimize(model, primary)
        gap = max(0, cost - h.getInfo().mip_dual_bound)
        return ReserveResult(model.network, cost, gap)

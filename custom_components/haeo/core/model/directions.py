"""Certified LP fast path and exact fallback for paired converter directions."""

from collections.abc import Mapping
import math

from highspy import Highs, HighsModelStatus, HighsVarType
from highspy.highs import highs_cons
import numpy as np
from numpy.typing import NDArray

from .elements.connection import Connection
from .elements.segments.power_limit import PowerLimitSegment

_FLOW_TOLERANCE = 1e-7
_COST_TOLERANCE = 1e-8


class _Pair:
    """One physical converter, with source-side flows and valid input limits."""

    def __init__(self, solver: Highs, forward: Connection[str], reverse: Connection[str]) -> None:
        self.forward = forward
        self.reverse = reverse
        self.modes = solver.addVariables(forward.n_periods, lb=0, ub=1, out_array=True)
        self.rows: list[tuple[highs_cons, highs_cons]] = []
        self.limits = np.full((2, forward.n_periods), np.inf)

    def refresh(self, solver: Highs) -> None:
        for side, connection in enumerate((self.forward, self.reverse)):
            limits = np.full(connection.n_periods, np.inf)
            for segment in connection.segments.values():
                if not isinstance(segment, PowerLimitSegment) or segment.max_power is None:
                    continue
                maximum = np.broadcast_to(segment.max_power, connection.n_periods)
                for t, expression in enumerate(segment.total_power_in):
                    # The power limit can follow efficiency losses. Bound the
                    # source flows using the actual segment coefficients, not
                    # an extra input-side nameplate limit.
                    _, coefficients = solver.qsum([expression]).unique_elements()
                    gain = float(np.min(coefficients)) if len(coefficients) else 0.0
                    if gain > 0:
                        limits[t] = min(limits[t], maximum[t] / gain)
            self.limits[side] = limits

    def update_rows(self, solver: Highs) -> None:
        if not self.rows:
            for t in range(self.forward.n_periods):
                self.rows.append(
                    (
                        solver.addConstr(self.forward.total_power_in[t] <= math.inf),
                        solver.addConstr(self.reverse.total_power_in[t] <= math.inf),
                    )
                )
        for t, (forward_row, reverse_row) in enumerate(self.rows):
            forward, reverse = self.limits[:, t]
            mode = self.modes[t].index
            solver.changeCoeff(forward_row.index, mode, -forward if math.isfinite(forward) else 0.0)
            solver.changeRowBounds(forward_row.index, -math.inf, 0.0 if math.isfinite(forward) else math.inf)
            solver.changeCoeff(reverse_row.index, mode, reverse if math.isfinite(reverse) else 0.0)
            solver.changeRowBounds(reverse_row.index, -math.inf, reverse)

    def opposing(self, solver: Highs) -> bool:
        return bool(
            np.any(
                np.minimum(solver.vals(self.forward.total_power_in), solver.vals(self.reverse.total_power_in))
                > _FLOW_TOLERANCE
            )
        )


class DirectionSolver:
    """Enforce paired directions without making every solve a MILP.

    The original relaxation is a lower bound. A feasible fixed-direction LP
    candidate is accepted only within the caller's horizon-wide cost tolerance.
    Otherwise all heuristic fixings are removed before the exact integer solve.
    Native lexicographic phases each use the same guarded solve, so secondary
    objectives cannot reintroduce circulation after the economic solve.
    """

    def __init__(self, solver: Highs) -> None:
        """Own direction rows and reversible choices on this solver instance."""
        self.solver = solver
        self.pairs: dict[str, _Pair] = {}
        self.gap = 0.0
        self.objective_constraint: highs_cons | None = None
        self._fixed: dict[int, tuple[float, float]] = {}
        self._integer = False

    def prepare(self, connections: Mapping[str, Connection[str]]) -> None:
        """Refresh limits once per input snapshot, retaining stable rows/columns."""
        self._reset()
        for name, forward in connections.items():
            reverse = connections.get(forward.exclusive_with) if forward.exclusive_with is not None else None
            if reverse is None:  # Policy compilation can prune either direction.
                continue
            if forward.source != reverse.target or forward.target != reverse.source:
                msg = f"Exclusive connections '{name}' and '{reverse.name}' must have opposite endpoints"
                raise ValueError(msg)
            if name not in self.pairs:
                self.pairs[name] = _Pair(self.solver, forward, reverse)
        for pair in self.pairs.values():
            pair.refresh(self.solver)

    def _reset(self) -> None:
        h = self.solver
        for col, (lower, upper) in self._fixed.items():
            h.changeColBounds(col, lower, upper)
        self._fixed.clear()
        for pair in self.pairs.values():
            for rows in pair.rows:
                for row in rows:
                    h.changeRowBounds(row.index, -math.inf, math.inf)
        if self._integer:
            for pair in self.pairs.values():
                for mode in pair.modes:
                    h.changeColIntegrality(mode.index, HighsVarType.kContinuous)
            self._integer = False

    def _fix_zero(self, connection: Connection[str], period: int) -> None:
        h = self.solver
        for flow in connection.power_in.values():
            col = flow[period].index
            if col not in self._fixed:
                _, _, lower, upper, _ = h.getCol(col)
                self._fixed[col] = (lower, upper)
            h.changeColBounds(col, self._fixed[col][0], 0.0)

    def _candidate(self) -> None:
        h = self.solver
        choices: list[tuple[_Pair, NDArray[np.bool_]]] = []
        for pair in self.pairs.values():
            # Compare on one bus: forward input is DC, reverse output is DC.
            # Public active power subtracts inputs on different buses instead.
            net_dc = h.vals(pair.forward.total_power_in) - h.vals(pair.reverse.total_power_out)
            choices.append((pair, net_dc >= 0.0))
        for pair, forward in choices:
            for t, inverting in enumerate(forward):
                self._fix_zero(pair.reverse if inverting else pair.forward, t)

    def _complete_limits(self) -> None:
        """Derive missing big-M bounds by optimization, never arbitrary constants."""
        h = self.solver
        objective, sense = h.getObjective()
        bounds = h.getExpr(self.objective_constraint).bounds if self.objective_constraint is not None else None
        if self.objective_constraint is not None:
            h.changeRowBounds(self.objective_constraint.index, -math.inf, math.inf)
        try:
            for pair in self.pairs.values():
                for side, connection in enumerate((pair.forward, pair.reverse)):
                    other = pair.reverse if side == 0 else pair.forward
                    for t in np.flatnonzero(~np.isfinite(pair.limits[side])):
                        self._fix_zero(other, int(t))
                        h.setObjective(-connection.total_power_in[t])
                        h.run()
                        status = h.getModelStatus()
                        if status == HighsModelStatus.kInfeasible:
                            maximum = 0.0  # This direction is impossible with its opposite off.
                        elif status == HighsModelStatus.kOptimal:
                            bound = h.getObjectiveValue()
                            if any(kind != HighsVarType.kContinuous for kind in h.getLp().integrality_):
                                _, bound = h.getInfoValue("mip_dual_bound")
                            maximum = max(0.0, -bound)
                        else:
                            msg = f"Cannot bound exclusive flow '{connection.name}'; configure a finite power limit"
                            raise ValueError(msg)
                        pair.limits[side, t] = maximum + _FLOW_TOLERANCE * max(1.0, maximum)
                        self._reset()
        finally:
            self._reset()
            h.setObjective(objective, sense)
            if self.objective_constraint is not None and bounds is not None:
                h.changeRowBounds(self.objective_constraint.index, *bounds)

    def run(self, *, absolute_tolerance: float = 0.0) -> None:
        """Run one objective with physical directions and a certified cost gap."""
        h = self.solver
        self._reset()
        self.gap = 0.0
        if not self.pairs:
            h.run()
            return
        _, original_solver = h.getOptionValue("solver")
        _, original_relative_gap = h.getOptionValue("mip_rel_gap")
        _, original_absolute_gap = h.getOptionValue("mip_abs_gap")
        try:
            # Other discrete policies already require a MILP. Avoid adding two
            # speculative solves to that path; keep their existing tolerance.
            other_integers = any(kind != HighsVarType.kContinuous for kind in h.getLp().integrality_)
            h.setOptionValue("solver", "choose" if other_integers else str(original_solver))
            if not other_integers:
                h.run()
                status = h.getModelStatus()
                if status == HighsModelStatus.kOptimal:
                    lower_bound = h.getObjectiveValue()
                    if not any(pair.opposing(h) for pair in self.pairs.values()):
                        return
                    self._candidate()
                    h.run()
                    if h.getModelStatus() == HighsModelStatus.kOptimal:
                        self.gap = max(0.0, h.getObjectiveValue() - lower_bound)
                        if self.gap <= absolute_tolerance + _COST_TOLERANCE:
                            return
                    self._reset()
                elif status not in (HighsModelStatus.kUnbounded, HighsModelStatus.kUnboundedOrInfeasible):
                    return

            h.setOptionValue("solver", "choose")
            h.setOptionValue("mip_rel_gap", 0.0)
            h.setOptionValue("mip_abs_gap", absolute_tolerance)
            self._complete_limits()
            for pair in self.pairs.values():
                pair.update_rows(h)
                for mode in pair.modes:
                    h.changeColIntegrality(mode.index, HighsVarType.kInteger)
            self._integer = True
            h.run()
            if h.getModelStatus() == HighsModelStatus.kOptimal:
                _, lower_bound = h.getInfoValue("mip_dual_bound")
                self.gap = max(0.0, h.getObjectiveValue() - lower_bound)
        finally:
            h.setOptionValue("solver", str(original_solver))
            h.setOptionValue("mip_rel_gap", original_relative_gap)
            h.setOptionValue("mip_abs_gap", original_absolute_gap)


__all__ = ["DirectionSolver"]

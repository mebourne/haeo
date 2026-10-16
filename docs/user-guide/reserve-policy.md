# Experimental battery reserve policy

This fork adds an **opt-in** reserve planner to the existing HAEO integration.
The `patched` installation is based on upstream **v0.4.1** and intended for supervised use with external price/SOC limits, not unattended direct dispatch.
It replaces the development-main-based dev3 build that encountered a config migration failure, as well as the rejected dev1/dev2 policies.
HAEO still does not control any device.
Your existing input entities, power/SOC forecasts and graphs remain the interface.

## Install and roll back

1. Make a full Home Assistant backup before replacing HAEO.
2. In HACS, add `https://github.com/mebourne/haeo` as a custom **Integration** repository.
    Select the **`patched`** branch in the download/redownload dialog, not an inherited upstream release.
    `patched` is this fork's default branch; its HACS metadata downloads source rather than a nonexistent release ZIP.
    Replace the upstream HACS download with the fork; do not delete your configured HAEO integration or run two copies of the same domain.
3. Restart Home Assistant and verify HAEO loads without setup errors.
4. Open **Settings → Devices & services → HAEO → Configure** and expand **Experimental battery reserve policy**.
    Leave it disabled initially to confirm your existing inputs and forecasts still work.
5. Check the battery settings below, enable the policy and submit.
    Saving hub options reloads the integration; no new HAEO instance is needed.

This build retains the release's nodes, custom tiers, input stores, adapters and output conventions.
It does not run the unreleased version 1.4 migration.
The one-off development-migration repair is not included in the normal reserve build.
If your configuration is still damaged by that migration, first recover using the separate [release-repair branch and instructions](https://github.com/mebourne/haeo/blob/release-repair/docs/user-guide/troubleshooting.md#returning-from-a-development-build-to-v041).
After successful recovery, the saved configuration uses the release schema and does not need the repair on subsequent starts.
Restoring an older damaged backup may require the repair again.
Disabling the reserve policy returns to this build's native LP and preserves your stored battery costs.
The reserve option does not migrate your configuration.
Keep your pre-install backup: custom settings already discarded by the development migration cannot be reconstructed without one.

For a manual install, download the `patched` source archive and replace only `custom_components/haeo` with that directory from the archive, then restart.
The archive includes the built frontend card files; it does not require a frontend build on Home Assistant.

## Supported configuration

The MVP supports exactly one battery connected to one inverter's DC bus, and one grid connected to the same AC junction as the inverter.
Fixed household loads connect to AC; fixed inverter overhead and solar connect to DC.
Solar curtailment and existing pricing policies are supported.
Extra storage, generators, controllable loads and arbitrary connecting networks are not supported by this policy.
Unsupported inputs produce an explicit optimization failure instead of silently reverting to an unprotected plan.

Use the existing battery partition settings:

| Setting                       | Example               | Meaning                                                                    |
| ----------------------------- | --------------------- | -------------------------------------------------------------------------- |
| Limits: minimum charge level  | 30%                   | Preferred minimum protecting ordinary battery sales                        |
| Undercharge: percentage       | 10%                   | Exact hard physical/model floor                                            |
| Undercharge: cost             | Existing value        | Kept in configuration, not used while the policy is enabled                |
| Limits: maximum charge level  | 95%                   | Preferred upper level                                                      |
| Overcharge: percentage / cost | 100% / existing value | Hard ceiling and the unchanged native hourly holding cost from 95% to 100% |

Rated capacity and these SOC boundaries must be constant over the horizon.
Both directional power limits must be finite and configured for the battery, inverter and grid.
Efficiencies, tariff forecasts, load/solar forecasts, salvage value and wear policies come from your configured inputs, not hard-coded plant ratings.
The policy does not change device-side backup cutoffs.
Power forecasts retain v0.4.1 measurement conventions: connection inputs are reported, while directional power limits apply at their configured segments after losses.
SOC forecasts retain all period boundaries, including the initial one.

## Hub options

| Option                        | Default | Interpretation                                                              |
| ----------------------------- | ------- | --------------------------------------------------------------------------- |
| Enable battery reserve policy | Off     | Keep ordinary HAEO behavior unless explicitly enabled                       |
| Reserve export price cutoff   | \$4/kWh | Current **or forecast** export prices at/above this qualify for reserve use |

These are the only reserve options.
There is no reserve import price cutoff, recovery margin or optimization time limit.
Prices use the configured currency per kWh, matching HAEO's tariff inputs.
The cutoff permits reserve use; it does not force an immediate export.
Previously saved obsolete settings and recovery records are ignored.
Submitting the reserve options removes obsolete settings from the configuration.

## Planning behavior

Reserve energy is the stock between the hard floor (10%) and the preferred minimum (30%).
One economic solve jointly optimizes all battery charging, local supply, solar and exports.
Local supply can come from the battery or from the grid at any forecast tariff.
It uses the native physical limits and your economic prices, with no invented reserve fee.

- A \$20/kWh export now can sell down to the hard 10% floor when buying household supply later, for example at \$10/kWh, is still more profitable.
- Refilling the battery can be delayed when a later import, for example at \$0.10/kWh, is preferable to refilling immediately.
- Under true scarcity, the economic objective may save energy for \$20 later instead of taking \$4 now.
- Reserve depletion creates no hourly lower holding penalty or replacement fee that forces expensive restoration to 30%.

**Conditional ordinary-sale guard.**
A below-cutoff battery sale is permitted only if every subsequent forecast boundary stays at or above the preferred minimum.
That includes later household consumption and qualifying export periods, not just the SOC immediately after the ordinary sale.
A later refill cannot excuse an earlier dip.
If a valuable forecast needs stock below 30%, the optimizer can omit the preceding ordinary sale and use down to the hard 10% floor for that opportunity.
This is stricter than merely preserving the reserve trajectory of a separate qualifying-only reference plan.

Household supply alone never triggers this guard.
Without ordinary battery sales, household consumption can use energy below 30%, and the battery need not be refilled to 30%.
Solar-only surplus may also export below 30% without forcing a refill.
Battery use that frees extra solar for sale counts as a battery-assisted sale, so source-tag routing cannot evade the guard.
When starting below 30%, economic charging may enable a later ordinary sale; earlier low SOC is not retroactively prohibited.

Charging has no reserve-specific price, debt, target or deadline.
It can change economically to support a profitable trade, but removing the trade also removes its conditional preferred-floor obligation.
All quantities, household supply and charging remain joint decisions; qualifying exports are not frozen by a preliminary solve.

Physical battery, inverter and grid opposing directions are exclusive in reserve mode.
The conditional sale guard and opposing directions use binary variables, so the solve is a mixed-integer program, **not a pure LP**.
There is no separate reserve-inventory model, qualifying-release marker, certificate or reference solve.

The solve accepts a \$0.001 absolute economic-objective gap with no relative gap.
This bounds the gap to the best predicted economic schedule within this conditional policy, not realized revenue or alternative reserve policies.
It does not relax SOC or physical constraints.
There is no solve time cancellation.
The native LP's calibrated tie-breaking is not applied to this MILP.

Reserve mode builds disposable models for each optimization.
With the policy disabled, the native warm-started LP is untouched.

## Stateless replanning

Each optimization uses current SOC and current forecasts, with no recovery history or persisted sale permission.
The conditional sale markers exist only within that forecast solve and are discarded afterward.
Reloading identical inputs does not change household-use permission.

## Outputs, failures and control limits

The same grid import/export, battery charge/discharge, energy and SOC entities publish native `forecast` attributes.
Constraint shadow prices and load marginal-price summaries remain unknown/unavailable in reserve mode because the MILP has no valid LP duals.
They must not be interpreted as zero opportunity cost.
Native-mode shadow prices are unchanged.

Each solve uses the inputs, SOC, tariffs and horizon captured at its start.
Previously published values and `forecast` attributes stay available while it runs.
New input events do not change or invalidate that solve: its completed result replaces the published outputs, then one follow-up solve processes the latest pending inputs after the configured debounce.
This applies with reserve enabled or disabled; intermediate input events are coalesced rather than replayed individually.
Horizon period durations are updated before solving, never underneath a running solve, using the targeted upstream horizon fix backported onto v0.4.1.

Invalid input or an infeasible model produces an optimization failure, not a native unprotected fallback.
On a real failure the current power forecast sensors become unavailable and their `forecast` attributes are dropped.
Do not rely on cached attributes surviving a failure; templates and macros that read them can become unknown.

**A forecast is not an actuator guard.**
Binary plant modes, four-hour price extrema, ≥5 kW filtering and filtered SOC thresholds do not reproduce an exact scheduled export quantity or the planned reserve-stock trajectory.
Frequent replanning helps, but cannot guarantee the optimizer's protected trajectory if the external controller deviates from it.
Retain manual bounds, queued uninterrupted actuator writes and graph supervision.

## Cached spike-price comparison

For the existing priority spike branch, this optional replacement preserves immediate reaction to actual price without waiting for a new solve:

```jinja
{{ grid_sell_price >= [
    manual_spike_sell_price,
    states('sensor.forecast_trade_sell_price')
      | float(manual_spike_sell_price) | round(2)
  ] | max }}
```

Keep the existing spike SOC condition and branch order, the two-second ordinary debounce and the queued actuator automation.
The existing automation already watches changes to the derived sell-price sensor.
Do not use its ordinary `auto_sell_price` variable's unknown fallback of 100 here: that would suppress the intended manual-only fallback.

This comparison deliberately falls back to the manual spike cutoff if no usable sell limit exists, including after a failure drops forecast attributes.
That favors capturing unexpected opportunities, but **unknown is not evidence that reserve discharge is safe**.
The existing four-hour ≥5 kW minimum sell-price summary may also expose a cheaper qualifying event even when an optimal schedule saves energy for a later, higher price.
Thus the comparison improves some decisions but does not exactly execute the reserve allocation.
Neither this automation edit, forecast-boundary pre-export nor advance negative-import solar curtailment is installed by the fork.

## Next steps

<div class="grid cards" markdown>

- :material-chart-line:{ .lg .middle } **Observe the forecasts**

    ---

    Check SOC, imports, exports and optimization duration after enabling, especially at a spike or below the preferred minimum.

    [:material-arrow-right: Understanding results](optimization.md)

- :material-battery:{ .lg .middle } **Keep existing battery limits**

    ---

    Confirm your physical floor and partition settings before experimenting.

    [:material-arrow-right: Battery settings](elements/battery.md)

</div>

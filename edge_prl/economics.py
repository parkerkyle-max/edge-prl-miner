"""Conditional CPU/NPU value comparison; never queries a pool or moves funds.

Rates must describe complete, nonstale work per elapsed wall-clock second.
The market inputs already include pool fees. Only the explicitly requested
developer fee is deducted, and only from this project's NPU PRL production.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import math
from pathlib import Path
import re
import sys
from typing import Any


class InputError(ValueError):
    """An input cannot support an unambiguous conditional comparison."""


THROUGHPUT_BASIS = "completed_nonstale_macs_per_wall_second"
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_INPUT_BYTES = 1024 * 1024


def _object(value: Any, path: str, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict:
        raise InputError(f"{path} must be an object")
    missing = required - value.keys()
    if missing:
        raise InputError(f"{path} is missing: {', '.join(sorted(missing))}")
    unknown = value.keys() - required - (optional or set())
    if unknown:
        raise InputError(f"{path} has unknown fields: {', '.join(sorted(map(str, unknown)))}")
    return value


def _number(value: Any, path: str) -> Decimal:
    # bool is a subclass of int; accepting it here hides malformed measurements.
    if type(value) not in (int, float):
        raise InputError(f"{path} must be a finite nonnegative JSON number")
    if type(value) is float and not math.isfinite(value):
        raise InputError(f"{path} must be finite")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise InputError(f"{path} must be finite and nonnegative")
    return number


def _optional_number(container: dict, key: str, path: str) -> Decimal | None:
    value = container.get(key)
    return None if value is None else _number(value, f"{path}.{key}")


def _timestamp(value: Any, path: str) -> datetime:
    if type(value) is not str or not _UTC_TIMESTAMP.fullmatch(value):
        raise InputError(f"{path} must be an explicit UTC ISO 8601 timestamp ending Z or +00:00")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{path} is not a valid calendar time") from exc


def _market(value: Any, coin: str, reward_key: str) -> dict:
    path = f"market.{coin}"
    item = _object(value, path, {
        "snapshot_id", "observed_at_utc", "cad_per_coin", reward_key, "after_pool_fee"
    })
    if type(item["snapshot_id"]) is not str or not _SNAPSHOT_ID.fullmatch(item["snapshot_id"]):
        raise InputError(f"{path}.snapshot_id must be a nonempty bounded snapshot identifier")
    if item["after_pool_fee"] is not True:
        raise InputError(f"{path}.after_pool_fee must be true; supply rates already net of pool fees")
    return {
        "snapshot_id": item["snapshot_id"],
        "observed_at_utc": _timestamp(item["observed_at_utc"], f"{path}.observed_at_utc"),
        "cad_per_coin": _number(item["cad_per_coin"], f"{path}.cad_per_coin"),
        "reward": _number(item[reward_key], f"{path}.{reward_key}"),
    }


def _json_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        converted = float(value)
        if not math.isfinite(converted):
            raise InputError("Calculated value exceeds the finite JSON number range")
        # A tiny positive number must not silently become a displayed zero.
        if value and not converted:
            raise InputError("Calculated value is below the finite nonzero JSON number range")
        return converted
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    return value


def evaluate(data: dict) -> dict:
    """Return a conditional CAD/day comparison, or raise InputError.

    A positive estimate is not proof of profit, pool credit, or payment. Missing
    wall power or electricity price produces null net values, never assumed zero.
    """
    root = _object(data, "input", {"schema_version", "baseline", "dual", "market"},
                   {"developer_fee_fraction", "electricity_cad_per_kwh"})
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise InputError("schema_version must be integer 1")
    baseline = _object(root["baseline"], "baseline", {"cpu_hps"}, {"wall_watts"})
    dual = _object(root["dual"], "dual", {"cpu_hps", "npu_effective_mac_s", "throughput_basis"},
                   {"wall_watts"})
    if dual["throughput_basis"] != THROUGHPUT_BASIS:
        raise InputError(f"dual.throughput_basis must be {THROUGHPUT_BASIS!r}; bare-kernel TOPS are not miner throughput")
    market = _object(root["market"], "market", {"prl", "xmr"})
    prl = _market(market["prl"], "prl", "reward_per_mac_day")
    xmr = _market(market["xmr"], "xmr", "reward_per_hash_day")
    if prl["snapshot_id"] != xmr["snapshot_id"]:
        raise InputError("PRL and XMR must use the same snapshot_id")
    if prl["observed_at_utc"] != xmr["observed_at_utc"]:
        raise InputError("PRL and XMR must use the same observed_at_utc snapshot time")
    baseline_hps = _number(baseline["cpu_hps"], "baseline.cpu_hps")
    dual_hps = _number(dual["cpu_hps"], "dual.cpu_hps")
    npu_mac_s = _number(dual["npu_effective_mac_s"], "dual.npu_effective_mac_s")
    fee = _number(root.get("developer_fee_fraction", 0.01), "developer_fee_fraction")
    if fee > Decimal("0.01"):
        raise InputError("developer_fee_fraction must be between 0 and 0.01")
    baseline_watts = _optional_number(baseline, "wall_watts", "baseline")
    dual_watts = _optional_number(dual, "wall_watts", "dual")
    electricity_price = _optional_number(root, "electricity_cad_per_kwh", "input")
    missing = []
    for key, value in (("baseline.wall_watts", baseline_watts), ("dual.wall_watts", dual_watts),
                       ("electricity_cad_per_kwh", electricity_price)):
        if value is None:
            missing.append(key)

    # Rates are coin/day for each unit of sustained per-second hashrate. There
    # is no extra factor of 86400: that day conversion is already in the rate.
    with localcontext() as context:
        context.prec = 50
        baseline_coins = baseline_hps * xmr["reward"]
        dual_cpu_coins = dual_hps * xmr["reward"]
        npu_coins = npu_mac_s * prl["reward"]
        baseline_value = baseline_coins * xmr["cad_per_coin"]
        dual_cpu_value = dual_cpu_coins * xmr["cad_per_coin"]
        npu_value = npu_coins * prl["cad_per_coin"]
        developer_coins = npu_coins * fee
        developer_value = npu_value * fee
        user_npu_value = npu_value - developer_value
        dual_value_before = dual_cpu_value + npu_value
        dual_value_after = dual_cpu_value + user_npu_value

        def cost(watts: Decimal | None) -> Decimal | None:
            if watts is None or electricity_price is None:
                return None
            return watts * Decimal(24) / Decimal(1000) * electricity_price

        baseline_cost, dual_cost = cost(baseline_watts), cost(dual_watts)
        extra_watts = None if baseline_watts is None or dual_watts is None else dual_watts - baseline_watts
        extra_cost = None if baseline_cost is None or dual_cost is None else dual_cost - baseline_cost
        gross_delta = dual_value_after - baseline_value
        net_delta = None if extra_cost is None else gross_delta - extra_cost
        result = {
            "schema_version": 1,
            "status": "CONDITIONAL_ESTIMATE_NOT_PAYOUT",
            "currency": "CAD",
            "period": "day",
            "snapshot": {
                "snapshot_id": prl["snapshot_id"],
                "observed_at_utc": prl["observed_at_utc"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            "throughput_basis": THROUGHPUT_BASIS,
            "developer_fee_fraction": fee,
            "baseline": {
                "cpu_xmr_coins_per_day": baseline_coins,
                "production_value_cad_per_day": baseline_value,
                "electricity_cad_per_day": baseline_cost,
                "net_cad_per_day": None if baseline_cost is None else baseline_value - baseline_cost,
            },
            "dual": {
                "cpu_xmr_coins_per_day": dual_cpu_coins,
                "cpu_value_cad_per_day": dual_cpu_value,
                "npu_prl_coins_per_day_before_devfee": npu_coins,
                "npu_value_cad_per_day_before_devfee": npu_value,
                "developer_fee_prl_per_day": developer_coins,
                "developer_fee_cad_per_day": developer_value,
                "npu_value_cad_per_day_after_devfee": user_npu_value,
                "total_value_cad_per_day_before_devfee": dual_value_before,
                "total_value_cad_per_day_after_devfee": dual_value_after,
                "electricity_cad_per_day": dual_cost,
                "net_cad_per_day": None if dual_cost is None else dual_value_after - dual_cost,
            },
            "comparison": {
                "cpu_value_loss_cad_per_day": baseline_value - dual_cpu_value,
                "gross_delta_cad_per_day_before_devfee": dual_value_before - baseline_value,
                "gross_delta_cad_per_day_after_devfee": gross_delta,
                "incremental_wall_watts": extra_watts,
                "incremental_electricity_cad_per_day": extra_cost,
                "net_delta_cad_per_day": net_delta,
                "estimate_positive_after_electricity": None if net_delta is None else net_delta > 0,
            },
            "unknown_inputs_for_net_delta": missing,
            "assumptions": [
                "Both market reward rates are already net of pool fees; no pool fee is deducted again.",
                "Reward rates mean coins/day per unit of sustained MAC/s (PRL) or H/s (XMR).",
                "Developer fee applies only to NPU PRL, not to the separate CPU miner.",
                "Input measurements and market quotes are supplied by the caller, not independently verified here.",
                "A constant snapshot and sustained observed rates are assumed for 24 hours; this is not actual payout.",
                "No revenue is inferred from accepted-share count; payout thresholds, price changes and downtime remain external.",
                "Only measured board wall-power cost is included; external compiler/host cost and hardware costs are excluded.",
            ],
        }
    return _json_numbers(result)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise InputError(f"Non-finite JSON constant is not allowed: {value}")


def load_input(path: Path) -> dict:
    """Load strict JSON with a modest size limit and duplicate-key rejection."""
    with Path(path).open("rb") as handle:
        raw = handle.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise InputError("Input JSON exceeds 1 MiB")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InputError("Input must be valid UTF-8 JSON") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate CPU-only versus NPU+CPU value from explicit measurements.")
    parser.add_argument("input_json", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(load_input(args.input_json))
    except (InputError, OSError, OverflowError) as exc:
        parser.exit(2, f"economics: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Unit/accounting checks; no hardware, wallet, pool, or network is used."""
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from edge_prl.economics import InputError, THROUGHPUT_BASIS, evaluate, load_input


def fixture():
    return {
        "schema_version": 1,
        "baseline": {"cpu_hps": 300, "wall_watts": 5},
        "dual": {"cpu_hps": 280, "npu_effective_mac_s": 5_000_000_000,
                 "throughput_basis": THROUGHPUT_BASIS, "wall_watts": 6},
        "market": {
            "prl": {"snapshot_id": "synthetic-unit-test", "observed_at_utc": "2026-01-01T00:00:00Z",
                    "reward_per_mac_day": 1e-14, "cad_per_coin": 2, "after_pool_fee": True},
            "xmr": {"snapshot_id": "synthetic-unit-test", "observed_at_utc": "2026-01-01T00:00:00+00:00",
                    "reward_per_hash_day": 1e-7, "cad_per_coin": 400, "after_pool_fee": True},
        },
        "developer_fee_fraction": 0.01,
        "electricity_cad_per_kwh": 0.12,
    }


class EconomicsTests(unittest.TestCase):
    def test_dimensions_cpu_loss_fee_and_incremental_energy(self):
        result = evaluate(fixture())
        self.assertAlmostEqual(result["baseline"]["production_value_cad_per_day"], .012)
        self.assertAlmostEqual(result["dual"]["npu_prl_coins_per_day_before_devfee"], .00005)
        self.assertAlmostEqual(result["dual"]["developer_fee_cad_per_day"], .000001)
        self.assertAlmostEqual(result["dual"]["total_value_cad_per_day_after_devfee"], .011299)
        self.assertAlmostEqual(result["comparison"]["cpu_value_loss_cad_per_day"], .0008)
        self.assertAlmostEqual(result["baseline"]["electricity_cad_per_day"], .0144)
        self.assertAlmostEqual(result["comparison"]["incremental_electricity_cad_per_day"], .00288)
        self.assertAlmostEqual(result["comparison"]["net_delta_cad_per_day"], -.003581)
        self.assertFalse(result["comparison"]["estimate_positive_after_electricity"])
        self.assertEqual(result["status"], "CONDITIONAL_ESTIMATE_NOT_PAYOUT")

    def test_fee_never_charges_cpu_miner(self):
        data = fixture()
        charged = evaluate(data)
        data["developer_fee_fraction"] = 0
        free = evaluate(data)
        self.assertEqual(charged["dual"]["cpu_value_cad_per_day"], free["dual"]["cpu_value_cad_per_day"])
        difference = free["dual"]["total_value_cad_per_day_after_devfee"] - charged["dual"]["total_value_cad_per_day_after_devfee"]
        self.assertAlmostEqual(difference, charged["dual"]["developer_fee_cad_per_day"])

    def test_default_fee_is_one_percent_and_input_is_not_mutated(self):
        data = fixture()
        del data["developer_fee_fraction"]
        original = copy.deepcopy(data)
        result = evaluate(data)
        self.assertEqual(result["developer_fee_fraction"], 0.01)
        self.assertEqual(data, original)

    def test_missing_or_null_power_and_price_stay_unknown(self):
        for section, field in (("baseline", "wall_watts"), ("dual", "wall_watts"), (None, "electricity_cad_per_kwh")):
            for null in (False, True):
                with self.subTest(section=section, field=field, null=null):
                    data = fixture()
                    target = data if section is None else data[section]
                    if null:
                        target[field] = None
                    else:
                        del target[field]
                    result = evaluate(data)
                    self.assertIsNone(result["comparison"]["net_delta_cad_per_day"])
                    self.assertIsNone(result["comparison"]["estimate_positive_after_electricity"])
                    self.assertTrue(result["unknown_inputs_for_net_delta"])
                    self.assertIsNotNone(result["comparison"]["gross_delta_cad_per_day_after_devfee"])

    def test_zero_electricity_is_a_measurement_not_unknown(self):
        data = fixture()
        data["electricity_cad_per_kwh"] = 0
        result = evaluate(data)
        self.assertEqual(result["unknown_inputs_for_net_delta"], [])
        self.assertEqual(result["comparison"]["net_delta_cad_per_day"], result["comparison"]["gross_delta_cad_per_day_after_devfee"])

    def test_zero_work_and_reduced_wall_power(self):
        data = fixture()
        data["dual"]["npu_effective_mac_s"] = 0
        data["dual"]["cpu_hps"] = data["baseline"]["cpu_hps"]
        data["dual"]["wall_watts"] = 4
        result = evaluate(data)
        self.assertEqual(result["dual"]["developer_fee_cad_per_day"], 0)
        self.assertAlmostEqual(result["comparison"]["net_delta_cad_per_day"], .00288)
        self.assertEqual(result["comparison"]["incremental_wall_watts"], -1)

    def test_invalid_numeric_measurements_and_fee(self):
        for value in (float("nan"), float("inf"), -float("inf"), -1, True, "300"):
            with self.subTest(value=value):
                data = fixture()
                data["baseline"]["cpu_hps"] = value
                with self.assertRaises(InputError):
                    evaluate(data)
        for fee in (-.01, .01000001, .02, float("nan"), True):
            data = fixture()
            data["developer_fee_fraction"] = fee
            with self.assertRaises(InputError):
                evaluate(data)

    def test_market_price_and_reward_are_required_finite_nonnegative(self):
        for coin, reward in (("prl", "reward_per_mac_day"), ("xmr", "reward_per_hash_day")):
            for field in (reward, "cad_per_coin"):
                for invalid in (None, -1, float("nan"), float("inf"), True):
                    data = fixture()
                    data["market"][coin][field] = invalid
                    with self.subTest(coin=coin, field=field, invalid=invalid), self.assertRaises(InputError):
                        evaluate(data)
                data = fixture()
                del data["market"][coin][field]
                with self.assertRaises(InputError):
                    evaluate(data)

    def test_snapshot_must_match_and_be_explicit_utc(self):
        for field, value in (("snapshot_id", "different"), ("snapshot_id", ""),
                             ("observed_at_utc", "2026-01-01T00:00:01Z"),
                             ("observed_at_utc", "2026-01-01T00:00:00"),
                             ("observed_at_utc", "2026-02-30T00:00:00Z"),
                             ("observed_at_utc", "2026-01-01T01:00:00+01:00")):
            data = fixture()
            data["market"]["prl"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(InputError):
                evaluate(data)

    def test_no_unknown_fields_or_ambiguous_pool_fees(self):
        data = fixture()
        data["dual"]["accepted_shares"] = 100
        with self.assertRaises(InputError):
            evaluate(data)
        for value in (False, 1, "true"):
            data = fixture()
            data["market"]["prl"]["after_pool_fee"] = value
            with self.assertRaises(InputError):
                evaluate(data)

    def test_bare_kernel_speed_and_noninteger_schema_rejected(self):
        data = fixture()
        data["dual"]["throughput_basis"] = "bare_vip_run_network"
        with self.assertRaises(InputError):
            evaluate(data)
        for version in (True, 1.0, "1", 2):
            data = fixture()
            data["schema_version"] = version
            with self.assertRaises(InputError):
                evaluate(data)

    def test_arithmetic_overflow_is_not_printed_as_infinity(self):
        data = fixture()
        data["dual"]["npu_effective_mac_s"] = 1e308
        data["market"]["prl"]["reward_per_mac_day"] = 1e308
        with self.assertRaises(InputError):
            evaluate(data)

    def test_strict_json_loader_and_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "comparison.json"
            for invalid in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}', '{"value":Infinity}'):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(InputError):
                    load_input(path)
            path.write_text(json.dumps(fixture()), encoding="utf-8")
            completed = subprocess.run([sys.executable, "-m", "edge_prl.economics", str(path)],
                                       cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                       text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertAlmostEqual(report["comparison"]["net_delta_cad_per_day"], -.003581)
            path.write_text('{"schema_version": NaN}', encoding="utf-8")
            rejected = subprocess.run([sys.executable, "-m", "edge_prl.economics", str(path)],
                                      cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                      text=True, timeout=10)
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")


if __name__ == "__main__":
    unittest.main()

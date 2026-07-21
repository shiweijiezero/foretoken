"""Unit tests for user-level throughput helpers."""

from __future__ import annotations

import unittest

from benchmarks.metrics.aggregator import (
    attach_user_throughput,
    tokens_per_s_per_user,
    user_count_for_throughput,
)
from benchmarks.report.summary import print_summary
from benchmarks.report.table import build_metrics_table
from benchmarks.storage.wandb_writer import (
    TOK_S_PER_USER_KEY,
    default_wandb_group_name,
    sweep_point_run_name,
)


class UserThroughputTests(unittest.TestCase):
    def test_open_loop_denominator(self) -> None:
        self.assertEqual(user_count_for_throughput(-1), 1)
        self.assertEqual(user_count_for_throughput(None), 1)
        self.assertEqual(user_count_for_throughput(8), 8)

    def test_tokens_per_s_per_user(self) -> None:
        self.assertAlmostEqual(tokens_per_s_per_user(80.0, 4), 20.0)
        self.assertAlmostEqual(tokens_per_s_per_user(80.0, -1), 80.0)

    def test_attach_user_throughput(self) -> None:
        metrics = {
            "throughput": {"token/s": 100.0, "request/s": 2.0},
        }
        attach_user_throughput(metrics, parallel=5)
        self.assertEqual(metrics["parallel"], 5)
        self.assertAlmostEqual(metrics["throughput"]["token/s/user"], 20.0)

    def test_metrics_table_includes_tok_s_user(self) -> None:
        table = build_metrics_table(
            client_metrics={
                "request_num": 10,
                "success_num": 10,
                "failed_num": 0,
                "success_rate": 1.0,
                "latency": {"mean": 1.0, "p50": 1.0, "p95": 1.0, "p99": 1.0},
                "throughput": {
                    "request/s": 1.0,
                    "token/s": 40.0,
                    "token/s/user": 10.0,
                },
                "benchmark_time": 10.0,
            }
        )
        names = [row[0] for row in table["rows"]]
        self.assertIn("token_per_s_per_user", names)

    def test_print_summary_includes_tok_s_user(self) -> None:
        metrics = {
            "request_num": 10,
            "success_num": 10,
            "failed_num": 0,
            "success_rate": 1.0,
            "parallel": 4,
            "latency": {"mean": 1.0, "p50": 1.0, "p95": 1.0, "p99": 1.0},
            "ttft": {"mean": 0.1, "p50": 0.1, "p95": 0.1, "p99": 0.1},
            "tpot": {"mean": 0.02, "p50": 0.02, "p95": 0.02, "p99": 0.02},
            "throughput": {
                "request/s": 1.0,
                "token/s": 40.0,
                "token/s/user": 10.0,
            },
            "benchmark_time": 10.0,
        }
        print_summary(
            {
                "model": "m",
                "resolved": {"parallel": 4, "number": 10},
                "stream": True,
            },
            metrics,
        )
        self.assertAlmostEqual(metrics["throughput"]["token/s/user"], 10.0)

    def test_sweep_point_naming(self) -> None:
        group = default_wandb_group_name(model="Qwen", run_name="exp1")
        self.assertEqual(group, "exp1")
        name = sweep_point_run_name(
            group, parallel=4, number=20, rate=5.0
        )
        self.assertEqual(name, "exp1-p4-n20-r5")
        open_name = sweep_point_run_name(
            group, parallel=-1, number=20, rate=-1.0, open_loop=True
        )
        self.assertEqual(open_name, "exp1-popen-n20-rinf")
        self.assertEqual(TOK_S_PER_USER_KEY, "Output Throughput per User (tok/s)")


if __name__ == "__main__":
    unittest.main()

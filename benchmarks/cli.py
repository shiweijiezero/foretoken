"""Typer entrypoint: register ``foretoken`` / ``foretoken bench`` commands."""

from __future__ import annotations

import typer

from benchmarks.config import bench, run_legacy, sweep_legacy

app = typer.Typer(name="foretoken", help="Foretoken LLM tools")
bench_app = typer.Typer(
    help="foretoken benchmarks",
    invoke_without_command=True,
)
app.add_typer(bench_app, name="bench")

bench_app.callback(invoke_without_command=True)(bench)
bench_app.command("run")(run_legacy)
bench_app.command("sweep")(sweep_legacy)


if __name__ == "__main__":
    app()

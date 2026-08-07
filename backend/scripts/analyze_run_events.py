"""只读统计 run_events 写放大画像（U2 Phase 0）。

对现有库输出四组数据，为文本增量微批参数与分区立项提供依据：

- 每 Run 帧数分布（p50 / p95 / max）与字节量分布；
- 按 event_type / category 分桶的行数与字节量；
- 总体规模（行数、字节量、Run 数）；
- 峰值插入速率（按秒聚合的最大帧数，近似值）。

数据已在表里，无需任何埋点；本脚本绝不写库。

用法::

    DATABASE_URL=postgresql+asyncpg://... uv run python scripts/analyze_run_events.py [--days 7]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


@dataclass(frozen=True)
class _Window:
    """Optional created_at lower bound shared by every query."""

    days: int | None

    @property
    def clause(self) -> str:
        if self.days is None:
            return ""
        return f" WHERE created_at >= now() - interval '{int(self.days)} days'"


async def _fetch_all(engine, query: str) -> list:
    async with engine.connect() as connection:
        result = await connection.execute(text(query))
        return list(result)


async def analyze(database_url: str, *, days: int | None) -> str:
    window = _Window(days=days)
    engine = create_async_engine(database_url, poolclass=NullPool)
    lines: list[str] = []
    scope = f"最近 {days} 天" if days is not None else "全部历史"
    lines.append(f"run_events 写放大画像（{scope}）")
    lines.append("=" * 60)
    try:
        totals = await _fetch_all(
            engine,
            "SELECT COUNT(*) AS rows, COALESCE(SUM(LENGTH(content)), 0) AS bytes, COUNT(DISTINCT run_id) AS runs FROM run_events" + window.clause,
        )
        total_rows, total_bytes, total_runs = totals[0]
        lines.append(f"总行数: {total_rows}    总内容字节: {total_bytes}    Run 数: {total_runs}")
        if total_rows == 0:
            lines.append("表为空 — 无可统计数据。")
            return "\n".join(lines)

        per_run = await _fetch_all(
            engine,
            (
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY frames) AS p50,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY frames) AS p95, MAX(frames) AS max,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY bytes) AS bytes_p50,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY bytes) AS bytes_p95, MAX(bytes) AS bytes_max"
                " FROM (SELECT run_id, COUNT(*) AS frames, SUM(LENGTH(content)) AS bytes"
                " FROM run_events" + window.clause + " GROUP BY run_id) AS per_run"
            ),
        )
        p50, p95, mx, bytes_p50, bytes_p95, bytes_max = per_run[0]
        lines.append("")
        lines.append(f"每 Run 帧数:  p50={p50:.0f}  p95={p95:.0f}  max={mx:d}")
        lines.append(f"每 Run 字节:  p50={bytes_p50:.0f}  p95={bytes_p95:.0f}  max={bytes_max:d}")

        buckets = await _fetch_all(
            engine,
            "SELECT category, event_type, COUNT(*) AS rows, SUM(LENGTH(content)) AS bytes FROM run_events" + window.clause + " GROUP BY category, event_type ORDER BY rows DESC",
        )
        lines.append("")
        lines.append(f"{'category':<12} {'event_type':<28} {'rows':>10} {'bytes':>14} {'rows%':>7}")
        lines.append("-" * 75)
        for category, event_type, rows, bytes_ in buckets:
            lines.append(f"{category:<12} {event_type:<28} {rows:>10} {bytes_:>14} {rows / total_rows:>7.1%}")

        peak = await _fetch_all(
            engine,
            "SELECT date_trunc('second', created_at) AS second, COUNT(*) AS frames FROM run_events" + window.clause + " GROUP BY second ORDER BY frames DESC LIMIT 1",
        )
        if peak:
            second, frames = peak[0]
            lines.append("")
            lines.append(f"峰值插入速率: {frames} 帧/秒（{second}）")
        return "\n".join(lines)
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读统计 run_events 帧数分布、事件分桶、字节量与峰值速率")
    parser.add_argument("--days", type=int, default=None, help="只统计最近 N 天（默认全部历史）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        print(asyncio.run(analyze(database_url, days=args.days)))
        return 0
    except Exception as exc:  # noqa: BLE001 — diagnostic CLI surfaces the reason directly
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

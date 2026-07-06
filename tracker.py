"""
Persists arbitrage trades and positions to SQLite for tracking and reporting.
"""

import aiosqlite
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "arbitrage.db"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    poly_question TEXT,
    kalshi_ticker TEXT,
    poly_side TEXT,
    kalshi_side TEXT,
    poly_price REAL,
    kalshi_price REAL,
    contracts INTEGER,
    total_cost REAL,
    expected_profit REAL,
    profit_pct REAL,
    poly_order_id TEXT,
    kalshi_order_id TEXT,
    poly_success INTEGER,
    kalshi_success INTEGER,
    match_score REAL,
    status TEXT DEFAULT 'open',
    dry_run INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    poly_markets_count INTEGER,
    kalshi_markets_count INTEGER,
    matched_count INTEGER,
    opportunities_count INTEGER,
    best_profit REAL,
    scan_duration_secs REAL
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


async def record_trade(trade_result) -> int:
    opp = trade_result.opportunity
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO trades (
                timestamp, poly_question, kalshi_ticker,
                poly_side, kalshi_side, poly_price, kalshi_price,
                contracts, total_cost, expected_profit, profit_pct,
                poly_order_id, kalshi_order_id,
                poly_success, kalshi_success, match_score,
                dry_run
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade_result.timestamp,
                opp.matched_market.polymarket.question,
                opp.kalshi_ticker,
                opp.poly_side,
                opp.kalshi_side,
                opp.poly_price,
                opp.kalshi_price,
                trade_result.contracts,
                opp.total_cost * trade_result.contracts,
                trade_result.expected_profit,
                opp.profit_pct,
                str(trade_result.poly_order) if trade_result.poly_order else None,
                str(trade_result.kalshi_order) if trade_result.kalshi_order else None,
                int(trade_result.poly_success),
                int(trade_result.kalshi_success),
                opp.matched_market.match_score,
                int(trade_result.opportunity.matched_market.polymarket.active),  # reuse as dry_run proxy
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def record_scan(
    poly_count: int,
    kalshi_count: int,
    matched: int,
    opportunities: int,
    best_profit: float,
    duration: float,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO scan_log (
                timestamp, poly_markets_count, kalshi_markets_count,
                matched_count, opportunities_count, best_profit, scan_duration_secs
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), poly_count, kalshi_count, matched, opportunities, best_profit, duration),
        )
        await db.commit()


async def get_recent_trades(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_trade_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN poly_success AND kalshi_success THEN 1 ELSE 0 END) as successful,
                SUM(expected_profit) as total_expected_profit,
                AVG(profit_pct) as avg_profit_pct,
                SUM(total_cost) as total_deployed
            FROM trades"""
        )
        row = await cursor.fetchone()
        if row:
            return {
                "total": row[0],
                "successful": row[1],
                "total_expected_profit": round(row[2] or 0, 4),
                "avg_profit_pct": round(row[3] or 0, 2),
                "total_deployed": round(row[4] or 0, 2),
            }
        return {"total": 0, "successful": 0, "total_expected_profit": 0, "avg_profit_pct": 0, "total_deployed": 0}

"""
Polymarket <-> Kalshi Arbitrage Bot

Scans both platforms for matching binary markets, identifies price
discrepancies where the combined cost of opposing sides < $1.00,
and executes simultaneous trades to lock in risk-free profit.

Polymarket auth: EIP-712 + POLY_GNOSIS_SAFE (signature_type=3) via py-clob-client-v2
Kalshi auth:     RSA-PSS signed headers (timestamp + method + path)
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime

from config import load_config
from polymarket_client import PolymarketClient
from kalshi_client import KalshiClient
from market_matcher import match_markets
from arbitrage_scanner import scan_for_arbitrage
from executor import Executor
import tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("arbitrage.log"),
    ],
)
logger = logging.getLogger("arbitrage-bot")

shutdown_event = asyncio.Event()


def handle_signal(sig, frame):
    logger.info("Shutdown signal received (%s)", signal.Signals(sig).name)
    shutdown_event.set()


async def run_cycle(
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
    executor: Executor,
    cfg,
) -> int:
    cycle_start = time.time()
    arb_cfg = cfg.arbitrage

    # Step 1: Fetch markets from both platforms
    logger.info("--- Scan cycle starting ---")
    poly_markets = poly_client.get_active_markets(limit=100, max_pages=5)
    kalshi_markets = kalshi_client.get_active_markets(limit=100, max_pages=5)

    if not poly_markets or not kalshi_markets:
        logger.warning("No markets fetched (Poly=%d, Kalshi=%d)", len(poly_markets), len(kalshi_markets))
        return 0

    # Step 2: Match markets across platforms
    logger.info(
        "Matching %d Polymarket x %d Kalshi markets...",
        len(poly_markets), len(kalshi_markets),
    )
    logger.info(
        "Sample Poly questions: %s",
        [m.question[:60] for m in poly_markets[:5]],
    )
    logger.info(
        "Sample Kalshi titles: %s",
        [m.title[:60] for m in kalshi_markets[:5]],
    )
    matched = match_markets(poly_markets, kalshi_markets, threshold=arb_cfg.match_score_threshold)
    if not matched:
        logger.info("No matching markets found between platforms")
        await tracker.record_scan(
            len(poly_markets), len(kalshi_markets), 0, 0, 0.0,
            time.time() - cycle_start,
        )
        return 0

    logger.info("Found %d matched markets, scanning for arbitrage...", len(matched))

    # Step 3: Scan for arbitrage opportunities
    opportunities = scan_for_arbitrage(
        matched, poly_client, kalshi_client,
        min_profit=arb_cfg.min_profit_threshold,
        refresh_prices=True,
    )

    best_profit = max((o.profit_per_contract for o in opportunities), default=0.0)
    await tracker.record_scan(
        len(poly_markets), len(kalshi_markets), len(matched),
        len(opportunities), best_profit, time.time() - cycle_start,
    )

    if not opportunities:
        logger.info("No arbitrage opportunities above threshold ($%.2f)", arb_cfg.min_profit_threshold)
        return 0

    # Step 4: Execute trades
    executed = 0
    for opp in opportunities:
        if shutdown_event.is_set():
            break

        result = executor.execute(opp)
        await tracker.record_trade(result)

        if result.poly_success and result.kalshi_success:
            executed += 1

    summary = executor.get_summary()
    logger.info(
        "Cycle complete: %d opportunities, %d executed | "
        "Total P&L: $%.4f | Exposure: $%.2f | Dry run: %s",
        len(opportunities), executed,
        summary["total_expected_profit"],
        summary["total_exposure"],
        summary["dry_run"],
    )

    return executed


async def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cfg = load_config()
    arb_cfg = cfg.arbitrage

    print("=" * 70)
    print("  POLYMARKET <-> KALSHI ARBITRAGE BOT")
    print("=" * 70)
    print(f"  Mode:             {'DRY RUN (no real trades)' if arb_cfg.dry_run else 'LIVE TRADING'}")
    print(f"  Min profit:       ${arb_cfg.min_profit_threshold:.2f} per contract")
    print(f"  Max bet size:     ${arb_cfg.max_bet_size:.2f} per side")
    print(f"  Max exposure:     ${arb_cfg.max_total_exposure:.2f}")
    print(f"  Match threshold:  {arb_cfg.match_score_threshold}%")
    print(f"  Scan interval:    {arb_cfg.scan_interval_seconds}s")
    print(f"  Kalshi env:       {'DEMO' if arb_cfg.use_kalshi_demo else 'PRODUCTION'}")
    print("=" * 70)

    # Initialize database
    await tracker.init_db()

    # Initialize clients
    poly_client = PolymarketClient(cfg.polymarket)
    kalshi_client_inst = KalshiClient(cfg.kalshi, use_demo=arb_cfg.use_kalshi_demo)

    try:
        poly_client.initialize()
    except Exception as e:
        logger.error("Failed to initialize Polymarket client: %s", e)
        logger.info("Continuing in read-only mode for Polymarket")

    try:
        kalshi_client_inst.initialize()
    except Exception as e:
        logger.error("Failed to initialize Kalshi client: %s", e)
        logger.info("Continuing in read-only mode for Kalshi")

    executor = Executor(poly_client, kalshi_client_inst, arb_cfg)

    cycle_count = 0
    while not shutdown_event.is_set():
        cycle_count += 1
        logger.info("=== Cycle %d at %s ===", cycle_count, datetime.now().strftime("%H:%M:%S"))

        try:
            await run_cycle(poly_client, kalshi_client_inst, executor, cfg)
        except Exception as e:
            logger.error("Cycle %d failed: %s", cycle_count, e, exc_info=True)

        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=arb_cfg.scan_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass

    # Print final summary
    summary = executor.get_summary()
    stats = await tracker.get_trade_stats()
    print("\n" + "=" * 70)
    print("  SHUTDOWN SUMMARY")
    print("=" * 70)
    print(f"  Cycles run:       {cycle_count}")
    print(f"  Trades attempted: {summary['total_trades']}")
    print(f"  Successful:       {summary['successful']}")
    print(f"  Partial fills:    {summary['partial_fills']}")
    print(f"  Expected profit:  ${summary['total_expected_profit']:.4f}")
    print(f"  Total exposure:   ${summary['total_exposure']:.2f}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

"""
Scans matched markets for cross-platform arbitrage opportunities.

An arbitrage opportunity exists when:
  Polymarket YES price + Kalshi NO price < $1.00  (buy YES on Poly, buy NO on Kalshi)
  OR
  Polymarket NO price + Kalshi YES price < $1.00  (buy NO on Poly, buy YES on Kalshi)

The guaranteed profit per contract = $1.00 - total_cost.

Two-pass approach:
  1. Fast scan using cached prices from initial market fetch
  2. Refresh prices only for the top candidates before execution
"""

import logging
from dataclasses import dataclass

from market_matcher import MatchedMarket
from polymarket_client import PolymarketClient
from kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    matched_market: MatchedMarket

    poly_side: str          # "YES" or "NO"
    poly_price: float       # price to pay on Polymarket
    poly_token_id: str      # token to buy on Polymarket

    kalshi_side: str        # "yes" or "no"
    kalshi_price: float     # price to pay on Kalshi
    kalshi_ticker: str      # ticker to buy on Kalshi

    total_cost: float       # poly_price + kalshi_price
    profit_per_contract: float  # 1.0 - total_cost
    profit_pct: float       # profit_per_contract / total_cost * 100

    def __str__(self) -> str:
        return (
            f"ARB: '{self.matched_market.polymarket.question[:50]}' | "
            f"Buy {self.poly_side} on Poly @ ${self.poly_price:.4f} + "
            f"Buy {self.kalshi_side} on Kalshi @ ${self.kalshi_price:.4f} = "
            f"${self.total_cost:.4f} | Profit: ${self.profit_per_contract:.4f} ({self.profit_pct:.2f}%)"
        )


def _find_opportunities_from_cached(
    matched_markets: list[MatchedMarket],
    min_profit: float,
) -> list[ArbitrageOpportunity]:
    opportunities = []

    for match in matched_markets:
        pm = match.polymarket
        km = match.kalshi

        # Strategy 1: Buy YES on Polymarket + Buy NO on Kalshi
        if pm.yes_price > 0 and km.no_ask > 0:
            cost = pm.yes_price + km.no_ask
            if cost < 1.0:
                profit = 1.0 - cost
                if profit >= min_profit:
                    opportunities.append(ArbitrageOpportunity(
                        matched_market=match,
                        poly_side="YES", poly_price=pm.yes_price,
                        poly_token_id=pm.yes_token_id,
                        kalshi_side="no", kalshi_price=km.no_ask,
                        kalshi_ticker=km.ticker,
                        total_cost=cost, profit_per_contract=profit,
                        profit_pct=(profit / cost) * 100,
                    ))

        # Strategy 2: Buy NO on Polymarket + Buy YES on Kalshi
        if pm.no_price > 0 and km.yes_ask > 0:
            cost = pm.no_price + km.yes_ask
            if cost < 1.0:
                profit = 1.0 - cost
                if profit >= min_profit:
                    opportunities.append(ArbitrageOpportunity(
                        matched_market=match,
                        poly_side="NO", poly_price=pm.no_price,
                        poly_token_id=pm.no_token_id,
                        kalshi_side="yes", kalshi_price=km.yes_ask,
                        kalshi_ticker=km.ticker,
                        total_cost=cost, profit_per_contract=profit,
                        profit_pct=(profit / cost) * 100,
                    ))

    opportunities.sort(key=lambda x: x.profit_per_contract, reverse=True)
    return opportunities


def scan_for_arbitrage(
    matched_markets: list[MatchedMarket],
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
    min_profit: float = 0.02,
    refresh_prices: bool = True,
    max_refresh: int = 20,
) -> list[ArbitrageOpportunity]:
    # Pass 1: fast scan with cached prices
    candidates = _find_opportunities_from_cached(matched_markets, min_profit)
    logger.info("Pass 1 (cached prices): %d candidates found", len(candidates))

    if not refresh_prices or not candidates:
        for opp in candidates:
            logger.info("Found opportunity: %s", opp)
        return candidates

    # Pass 2: refresh prices for the top candidates only
    top = candidates[:max_refresh]
    verified = []

    for opp in top:
        pm = opp.matched_market.polymarket
        token_id = opp.poly_token_id
        fresh = poly_client.get_price(token_id, "BUY")
        if fresh is None:
            continue

        if opp.poly_side == "YES":
            pm.yes_price = fresh
            cost = fresh + opp.kalshi_price
        else:
            pm.no_price = fresh
            cost = fresh + opp.kalshi_price

        if cost < 1.0:
            profit = 1.0 - cost
            if profit >= min_profit:
                opp.poly_price = fresh
                opp.total_cost = cost
                opp.profit_per_contract = profit
                opp.profit_pct = (profit / cost) * 100
                verified.append(opp)
                logger.info("Found opportunity: %s", opp)

    verified.sort(key=lambda x: x.profit_per_contract, reverse=True)
    logger.info(
        "Scan complete: %d verified opportunities (from %d candidates, min_profit=$%.2f)",
        len(verified), len(candidates), min_profit,
    )
    return verified

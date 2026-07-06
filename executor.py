"""
Executes arbitrage trades on both Polymarket and Kalshi simultaneously.

For each opportunity, places a limit order on both platforms. Tracks
total exposure and respects position limits.
"""

import logging
import math
import time
from dataclasses import dataclass, field

from arbitrage_scanner import ArbitrageOpportunity
from polymarket_client import PolymarketClient
from kalshi_client import KalshiClient
from config import ArbitrageConfig

logger = logging.getLogger(__name__)

POLYMARKET_MIN_SIZE = 5.0
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_FACTOR = 2


@dataclass
class TradeResult:
    opportunity: ArbitrageOpportunity
    poly_order: dict | None = None
    kalshi_order: dict | None = None
    poly_success: bool = False
    kalshi_success: bool = False
    contracts: int = 0
    expected_profit: float = 0.0
    error: str = ""
    timestamp: float = field(default_factory=time.time)


class Executor:
    def __init__(
        self,
        poly_client: PolymarketClient,
        kalshi_client: KalshiClient,
        cfg: ArbitrageConfig,
    ):
        self.poly = poly_client
        self.kalshi = kalshi_client
        self.cfg = cfg
        self.total_exposure = 0.0
        self.trade_history: list[TradeResult] = []

    def execute(self, opportunity: ArbitrageOpportunity) -> TradeResult:
        result = TradeResult(opportunity=opportunity)

        if self.cfg.dry_run:
            return self._dry_run(opportunity, result)

        remaining_capital = self.cfg.max_total_exposure - self.total_exposure
        if remaining_capital <= 0:
            result.error = "Max total exposure reached"
            logger.warning(result.error)
            return result

        max_spend_per_side = min(self.cfg.max_bet_size, remaining_capital / 2)
        contracts = self._calculate_contracts(opportunity, max_spend_per_side)

        if contracts < 1:
            result.error = "Position too small after sizing"
            logger.warning(result.error)
            return result

        poly_cost = contracts * opportunity.poly_price
        if poly_cost < POLYMARKET_MIN_SIZE:
            result.error = f"Polymarket order below minimum (${poly_cost:.2f} < ${POLYMARKET_MIN_SIZE})"
            logger.warning(result.error)
            return result

        result.contracts = contracts
        result.expected_profit = contracts * opportunity.profit_per_contract

        logger.info(
            "Executing arbitrage: %d contracts | Poly %s @ $%.4f | Kalshi %s @ $%.4f | Expected profit: $%.4f",
            contracts, opportunity.poly_side, opportunity.poly_price,
            opportunity.kalshi_side, opportunity.kalshi_price,
            result.expected_profit,
        )

        # Place Polymarket order
        poly_resp = self._place_poly_order(opportunity, contracts)
        if poly_resp:
            result.poly_order = poly_resp
            result.poly_success = True
        else:
            result.error = "Polymarket order failed"
            logger.error(result.error)
            self.trade_history.append(result)
            return result

        # Place Kalshi order
        kalshi_resp = self._place_kalshi_order(opportunity, contracts)
        if kalshi_resp:
            result.kalshi_order = kalshi_resp
            result.kalshi_success = True
        else:
            result.error = "Kalshi order failed (Polymarket order was placed - MANUAL INTERVENTION MAY BE NEEDED)"
            logger.error(result.error)

        if result.poly_success and result.kalshi_success:
            total_deployed = (contracts * opportunity.poly_price) + (contracts * opportunity.kalshi_price)
            self.total_exposure += total_deployed
            logger.info(
                "Arbitrage executed successfully! Deployed: $%.2f | Total exposure: $%.2f",
                total_deployed, self.total_exposure,
            )

        self.trade_history.append(result)
        return result

    def _dry_run(self, opp: ArbitrageOpportunity, result: TradeResult) -> TradeResult:
        remaining = self.cfg.max_total_exposure - self.total_exposure
        max_spend = min(self.cfg.max_bet_size, remaining / 2)
        contracts = self._calculate_contracts(opp, max_spend)

        result.contracts = contracts
        result.expected_profit = contracts * opp.profit_per_contract
        result.poly_success = True
        result.kalshi_success = True

        logger.info(
            "[DRY RUN] Would execute: %d contracts | "
            "Poly %s @ $%.4f ($%.2f) + Kalshi %s @ $%.4f ($%.2f) = "
            "Cost $%.2f | Profit $%.4f",
            contracts,
            opp.poly_side, opp.poly_price, contracts * opp.poly_price,
            opp.kalshi_side, opp.kalshi_price, contracts * opp.kalshi_price,
            contracts * opp.total_cost,
            result.expected_profit,
        )

        self.trade_history.append(result)
        return result

    def _calculate_contracts(self, opp: ArbitrageOpportunity, max_spend: float) -> int:
        max_by_poly = max_spend / opp.poly_price if opp.poly_price > 0 else 0
        max_by_kalshi = max_spend / opp.kalshi_price if opp.kalshi_price > 0 else 0
        return int(min(max_by_poly, max_by_kalshi))

    def _place_poly_order(self, opp: ArbitrageOpportunity, contracts: int) -> dict | None:
        slippage = 0.01
        price = round(opp.poly_price * (1 + slippage), 4)
        price = min(price, 0.99)

        for attempt in range(MAX_RETRY_ATTEMPTS):
            resp = self.poly.place_limit_order(
                token_id=opp.poly_token_id,
                side="BUY",
                price=price,
                size=float(contracts),
            )
            if resp:
                return resp

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                delay = RETRY_BACKOFF_FACTOR ** (attempt + 1)
                logger.warning("Polymarket order attempt %d failed, retrying in %ds", attempt + 1, delay)
                time.sleep(delay)

        return None

    def _place_kalshi_order(self, opp: ArbitrageOpportunity, contracts: int) -> dict | None:
        for attempt in range(MAX_RETRY_ATTEMPTS):
            resp = self.kalshi.place_limit_order(
                ticker=opp.kalshi_ticker,
                side=opp.kalshi_side,
                action="buy",
                count=contracts,
                price=opp.kalshi_price,
            )
            if resp:
                return resp

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                delay = RETRY_BACKOFF_FACTOR ** (attempt + 1)
                logger.warning("Kalshi order attempt %d failed, retrying in %ds", attempt + 1, delay)
                time.sleep(delay)

        return None

    def get_summary(self) -> dict:
        successful = [t for t in self.trade_history if t.poly_success and t.kalshi_success]
        failed = [t for t in self.trade_history if not (t.poly_success and t.kalshi_success)]
        partial = [t for t in self.trade_history if t.poly_success != t.kalshi_success]

        total_expected_profit = sum(t.expected_profit for t in successful)

        return {
            "total_trades": len(self.trade_history),
            "successful": len(successful),
            "failed": len(failed),
            "partial_fills": len(partial),
            "total_expected_profit": round(total_expected_profit, 4),
            "total_exposure": round(self.total_exposure, 2),
            "dry_run": self.cfg.dry_run,
        }

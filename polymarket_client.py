"""
Polymarket client for market data and order execution.

Uses py-clob-client-v2 for order signing/submission (EIP-712 + POLY_GNOSIS_SAFE),
and direct httpx calls for market discovery via the Gamma API.
"""

import json
import logging
import uuid
from dataclasses import dataclass

import httpx
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgsV2, OrderType, OpenOrderParams, OrderPayload
from py_clob_client_v2.order_builder.constants import BUY, SELL

from config import PolymarketConfig

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class PolymarketMarket:
    market_id: str
    question: str
    description: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    end_date: str
    tags: list
    active: bool


class PolymarketClient:
    def __init__(self, cfg: PolymarketConfig):
        self.cfg = cfg
        self.http = httpx.Client(timeout=120)
        self.clob: ClobClient | None = None
        self._initialized = False

    def initialize(self):
        if not self.cfg.private_key:
            logger.warning("No Polymarket private key configured - read-only mode")
            self._initialized = True
            return

        self.clob = ClobClient(
            CLOB_API,
            key=self.cfg.private_key,
            chain_id=self.cfg.chain_id,
            signature_type=self.cfg.signature_type,
            funder=self.cfg.wallet_address,
        )
        creds = self.clob.create_or_derive_api_key()
        self.clob.set_api_creds(creds)
        self._initialized = True
        logger.info("Polymarket client initialized (chain_id=%d, sig_type=%d)", self.cfg.chain_id, self.cfg.signature_type)

    def _parse_json_or_csv(self, value) -> list:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return [x.strip().strip('"') for x in value.split(",")]
        return []

    def get_active_markets(self, limit: int = 100, max_pages: int = 50) -> list[PolymarketMarket]:
        markets = []
        for page in range(max_pages):
            offset = page * limit
            try:
                resp = self.http.get(
                    f"{GAMMA_API}/markets",
                    params={"active": "true", "closed": "false", "limit": limit, "offset": offset},
                )
                resp.raise_for_status()
                batch = resp.json()
            except Exception as e:
                logger.error("Failed to fetch Polymarket markets page %d: %s", page, e)
                break

            if not batch:
                break

            for m in batch:
                parsed = self._parse_market(m)
                if parsed:
                    markets.append(parsed)

        logger.info("Fetched %d active Polymarket markets", len(markets))
        return markets

    def _parse_market(self, m: dict) -> PolymarketMarket | None:
        try:
            outcomes = self._parse_json_or_csv(m.get("outcomes", []))
            clob_ids = self._parse_json_or_csv(m.get("clobTokenIds", []))
            prices = self._parse_json_or_csv(m.get("outcomePrices", []))

            if len(outcomes) < 2 or len(clob_ids) < 2:
                return None

            yes_idx = None
            no_idx = None
            for i, o in enumerate(outcomes):
                if str(o).lower().strip('"') == "yes":
                    yes_idx = i
                elif str(o).lower().strip('"') == "no":
                    no_idx = i

            if yes_idx is None or no_idx is None:
                return None

            yes_price = float(prices[yes_idx]) if yes_idx < len(prices) else 0.0
            no_price = float(prices[no_idx]) if no_idx < len(prices) else 0.0

            tags_raw = m.get("tags", [])
            if isinstance(tags_raw, str):
                try:
                    tags_raw = json.loads(tags_raw)
                except Exception:
                    tags_raw = []

            return PolymarketMarket(
                market_id=str(m.get("id", "")),
                question=m.get("question", ""),
                description=m.get("description", ""),
                yes_token_id=clob_ids[yes_idx],
                no_token_id=clob_ids[no_idx],
                yes_price=yes_price,
                no_price=no_price,
                volume=float(m.get("volume", 0)),
                liquidity=float(m.get("liquidity", 0)),
                end_date=m.get("endDate", ""),
                tags=tags_raw if isinstance(tags_raw, list) else [],
                active=True,
            )
        except Exception as e:
            logger.debug("Failed to parse Polymarket market: %s", e)
            return None

    def get_price(self, token_id: str, side: str = "BUY") -> float | None:
        try:
            resp = self.http.get(
                f"{CLOB_API}/price",
                params={"token_id": token_id, "side": side},
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0))
        except Exception as e:
            logger.error("Failed to get Polymarket price for %s: %s", token_id, e)
            return None

    def get_orderbook(self, token_id: str) -> dict | None:
        try:
            resp = self.http.get(f"{CLOB_API}/order-book/{token_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to get Polymarket orderbook for %s: %s", token_id, e)
            return None

    def place_limit_order(self, token_id: str, side: str, price: float, size: float) -> dict | None:
        if not self.clob:
            logger.error("Cannot place order: Polymarket client not initialized with credentials")
            return None

        order_side = BUY if side.upper() == "BUY" else SELL
        order_args = OrderArgsV2(
            price=round(price, 4),
            size=size,
            side=order_side,
            token_id=token_id,
        )

        try:
            signed_order = self.clob.create_order(order_args)
            resp = self.clob.post_order(signed_order, OrderType.GTC)
            logger.info(
                "Polymarket order placed: %s %s @ %.4f x %.2f -> %s",
                side, token_id[:16], price, size, resp,
            )
            return resp
        except Exception as e:
            logger.error("Failed to place Polymarket order: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        if not self.clob:
            return False
        try:
            self.clob.cancel_order(OrderPayload(orderID=order_id))
            return True
        except Exception as e:
            logger.error("Failed to cancel Polymarket order %s: %s", order_id, e)
            return False

    def get_open_orders(self) -> list:
        if not self.clob:
            return []
        try:
            return self.clob.get_open_orders(OpenOrderParams())
        except Exception as e:
            logger.error("Failed to get Polymarket open orders: %s", e)
            return []

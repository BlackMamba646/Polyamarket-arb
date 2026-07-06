"""
Kalshi client for market data and order execution.

Uses RSA-PSS signing for authentication. Every request is signed with:
  signature = RSA_PSS_SIGN(timestamp_ms + HTTP_METHOD + path)
Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
"""

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import KalshiConfig

logger = logging.getLogger(__name__)

PROD_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    subtitle: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume: int
    open_interest: int
    status: str
    expiration_time: str
    event_ticker: str
    category: str


class KalshiClient:
    def __init__(self, cfg: KalshiConfig, use_demo: bool = True):
        self.cfg = cfg
        self.base_url = DEMO_URL if use_demo else PROD_URL
        self.private_key = None
        self.http = httpx.Client(timeout=120)
        self._initialized = False

    def initialize(self):
        if not self.cfg.private_key_path or not self.cfg.api_key_id:
            logger.warning("No Kalshi credentials configured - read-only mode (public endpoints only)")
            self._initialized = True
            return

        try:
            with open(self.cfg.private_key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(f.read(), password=None)
            self._initialized = True
            logger.info("Kalshi client initialized (base_url=%s)", self.base_url)
        except Exception as e:
            logger.error("Failed to load Kalshi private key: %s", e)
            raise

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict:
        if not self.private_key:
            return {"Content-Type": "application/json"}
        timestamp = str(int(time.time() * 1000))
        full_path = urlparse(self.base_url + path).path
        signature = self._sign(timestamp, method, full_path)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers("GET", path)
        resp = self.http.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers("POST", path)
        resp = self.http.post(url, headers=headers, json=data)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers("DELETE", path)
        resp = self.http.delete(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_active_markets(self, limit: int = 200, max_pages: int = 25) -> list[KalshiMarket]:
        """Fetch markets via the events endpoint to get real binary markets, not parlays."""
        markets = []
        seen_tickers = set()
        cursor = None

        for page in range(max_pages):
            try:
                params = {
                    "limit": limit,
                    "status": "open",
                    "with_nested_markets": "true",
                }
                if cursor:
                    params["cursor"] = cursor

                data = self._get("/events", params=params)
                batch = data.get("events", [])

                if not batch:
                    break

                for event in batch:
                    event_ticker = event.get("event_ticker", "")
                    # Skip multi-event parlays
                    if event_ticker.startswith("KXMVE"):
                        continue

                    event_title = event.get("title", "")
                    event_category = event.get("category", "")
                    nested_markets = event.get("markets", [])

                    for m in nested_markets:
                        if m.get("ticker", "") in seen_tickers:
                            continue
                        m["_event_title"] = event_title
                        m["_event_category"] = event_category
                        parsed = self._parse_market(m)
                        if parsed:
                            markets.append(parsed)
                            seen_tickers.add(parsed.ticker)

                cursor = data.get("cursor")
                if not cursor:
                    break

            except Exception as e:
                logger.error("Failed to fetch Kalshi events page %d: %s", page, e)
                break

        logger.info("Fetched %d active Kalshi markets (via events endpoint)", len(markets))
        return markets

    def _parse_market(self, m: dict) -> KalshiMarket | None:
        try:
            ticker = m.get("ticker", "")
            title = m.get("title", "") or m.get("_event_title", "")
            event_title = m.get("_event_title", "")

            # Skip multi-leg parlay/combo markets
            if ticker.startswith("KXMVE"):
                return None
            if title.count(",") >= 2 and ("yes " in title.lower() or "no " in title.lower()):
                return None

            yes_bid = self._parse_price(m.get("yes_bid_dollars") or m.get("yes_bid"))
            yes_ask = self._parse_price(m.get("yes_ask_dollars") or m.get("yes_ask"))

            if yes_bid is None and yes_ask is None:
                return None

            yes_bid = yes_bid or 0.0
            yes_ask = yes_ask or 1.0
            no_bid = round(1.0 - yes_ask, 4)
            no_ask = round(1.0 - yes_bid, 4)

            return KalshiMarket(
                ticker=m.get("ticker", ""),
                title=title,
                subtitle=event_title or m.get("subtitle", ""),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                volume=m.get("volume", 0),
                open_interest=m.get("open_interest", 0),
                status=m.get("status", ""),
                expiration_time=m.get("expiration_time", ""),
                event_ticker=m.get("event_ticker", ""),
                category=m.get("category", ""),
            )
        except Exception as e:
            logger.debug("Failed to parse Kalshi market: %s", e)
            return None

    def _parse_price(self, value) -> float | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1.0:
                return v / 100.0
            return v
        return None

    def get_orderbook(self, ticker: str) -> dict | None:
        try:
            return self._get(f"/markets/{ticker}/orderbook")
        except Exception as e:
            logger.error("Failed to get Kalshi orderbook for %s: %s", ticker, e)
            return None

    def get_balance(self) -> float | None:
        try:
            data = self._get("/portfolio/balance")
            balance = data.get("balance", 0)
            if isinstance(balance, (int, float)) and balance > 100:
                return balance / 100.0
            return float(balance)
        except Exception as e:
            logger.error("Failed to get Kalshi balance: %s", e)
            return None

    def place_limit_order(
        self, ticker: str, side: str, action: str, count: int, price: float
    ) -> dict | None:
        if not self.private_key:
            logger.error("Cannot place order: Kalshi client not initialized with credentials")
            return None

        # New V2 API: side is "bid" (buy YES) or "ask" (sell YES = buy NO)
        # Price is always the YES-side price
        if side.lower() == "yes" and action.lower() == "buy":
            book_side = "bid"
            api_price = f"{price:.4f}"
        elif side.lower() == "no" and action.lower() == "buy":
            book_side = "ask"
            api_price = f"{1.0 - price:.4f}"
        elif side.lower() == "yes" and action.lower() == "sell":
            book_side = "ask"
            api_price = f"{price:.4f}"
        else:
            book_side = "bid"
            api_price = f"{1.0 - price:.4f}"

        count_str = f"{count}.00"

        order = {
            "ticker": ticker,
            "side": book_side,
            "count": count_str,
            "price": api_price,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4()),
        }

        try:
            resp = self._post("/portfolio/events/orders", data=order)
            logger.info(
                "Kalshi order placed: %s %s %s @ $%s x %d -> %s",
                action, side, ticker, api_price, count, resp.get("order_id", "?"),
            )
            return resp
        except Exception as e:
            logger.error("Failed to place Kalshi order: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._delete(f"/portfolio/events/orders/{order_id}")
            return True
        except Exception as e:
            logger.error("Failed to cancel Kalshi order %s: %s", order_id, e)
            return False

    def get_positions(self) -> list:
        try:
            data = self._get("/portfolio/positions")
            return data.get("positions", [])
        except Exception as e:
            logger.error("Failed to get Kalshi positions: %s", e)
            return []

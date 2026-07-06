import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PolymarketConfig:
    private_key: str = ""
    wallet_address: str = ""
    chain_id: int = 137
    signature_type: int = 3
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"


@dataclass
class KalshiConfig:
    api_key_id: str = ""
    private_key_path: str = ""
    base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    demo_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"


@dataclass
class ArbitrageConfig:
    min_profit_threshold: float = 0.02
    max_bet_size: float = 50.0
    scan_interval_seconds: int = 30
    match_score_threshold: int = 80
    max_total_exposure: float = 500.0
    dry_run: bool = True
    use_kalshi_demo: bool = True


@dataclass
class Config:
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)


def load_config(path: str = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.yaml.example to config.yaml and fill in your credentials."
        )

    with open(p) as f:
        raw = yaml.safe_load(f)

    cfg = Config()

    if "polymarket" in raw:
        pm = raw["polymarket"]
        cfg.polymarket = PolymarketConfig(
            private_key=pm.get("private_key", ""),
            wallet_address=pm.get("wallet_address", ""),
            chain_id=pm.get("chain_id", 137),
            signature_type=pm.get("signature_type", 3),
        )

    if "kalshi" in raw:
        k = raw["kalshi"]
        cfg.kalshi = KalshiConfig(
            api_key_id=k.get("api_key_id", ""),
            private_key_path=k.get("private_key_path", ""),
        )

    if "arbitrage" in raw:
        a = raw["arbitrage"]
        cfg.arbitrage = ArbitrageConfig(
            min_profit_threshold=a.get("min_profit_threshold", 0.02),
            max_bet_size=a.get("max_bet_size", 50.0),
            scan_interval_seconds=a.get("scan_interval_seconds", 30),
            match_score_threshold=a.get("match_score_threshold", 80),
            max_total_exposure=a.get("max_total_exposure", 500.0),
            dry_run=a.get("dry_run", True),
            use_kalshi_demo=a.get("use_kalshi_demo", True),
        )

    return cfg

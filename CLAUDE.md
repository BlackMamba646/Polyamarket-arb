# Polymarket <-> Kalshi Arbitrage Bot

## What This Does
Scans Polymarket and Kalshi for matching binary prediction markets, finds price discrepancies
where the combined cost of opposing outcomes < $1.00, and executes simultaneous trades for
risk-free arbitrage profit.

## Architecture
- `main.py` - Main loop: scan -> match -> find arbitrage -> execute
- `polymarket_client.py` - Polymarket Gamma + CLOB API (py-clob-client-v2, EIP-712, signature_type=3)
- `kalshi_client.py` - Kalshi Trade API v2 (RSA-PSS signed headers)
- `market_matcher.py` - Fuzzy text matching of market questions across platforms
- `arbitrage_scanner.py` - Identifies cross-platform price discrepancies
- `executor.py` - Places orders on both platforms with retry logic
- `tracker.py` - SQLite persistence for trades and scan history
- `config.py` - YAML config loader

## Setup
```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Fill in credentials in config.yaml
python main.py
```

## Key Details
- Polymarket: chain_id=137 (Polygon), signature_type=3 (POLY_GNOSIS_SAFE), CLOB at clob.polymarket.com
- Kalshi: RSA-PSS auth, sign(timestamp+method+path), api.elections.kalshi.com/trade-api/v2
- Default: dry_run=true (no real trades)
- Min order on Polymarket: $5.00

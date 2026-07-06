"""
Matches markets between Polymarket and Kalshi using fuzzy text matching.

Uses a two-pass approach: first extracts keywords to pre-filter candidates,
then runs full fuzzy matching only on plausible pairs.
"""

import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from polymarket_client import PolymarketMarket
from kalshi_client import KalshiMarket

logger = logging.getLogger(__name__)

STOPWORDS = {
    "will", "the", "a", "an", "by", "on", "in", "at", "to", "of", "be",
    "is", "are", "was", "were", "has", "have", "had", "do", "does", "did",
    "this", "that", "it", "its", "for", "with", "from", "or", "and", "not",
    "than", "more", "less", "above", "below", "before", "after",
}


@dataclass
class MatchedMarket:
    polymarket: PolymarketMarket
    kalshi: KalshiMarket
    match_score: float
    match_method: str


def extract_keywords(text: str) -> set[str]:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    words = text.split()
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[''`]", "'", text)
    text = re.sub(r'["""]', '"', text)
    text = re.sub(r"[^\w\s'\".,?!%-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def match_markets(
    pm_markets: list[PolymarketMarket],
    kalshi_markets: list[KalshiMarket],
    threshold: int = 80,
) -> list[MatchedMarket]:
    if not pm_markets or not kalshi_markets:
        return []

    # Pre-compute keyword sets and normalized text for all Kalshi markets
    kalshi_data = []
    for km in kalshi_markets:
        combined = f"{km.title} {km.subtitle}"
        kalshi_data.append({
            "market": km,
            "keywords": extract_keywords(combined),
            "norm_title": normalize_text(km.title),
            "norm_combined": normalize_text(combined),
        })

    matches = []
    used_kalshi = set()

    for pm in pm_markets:
        pm_keywords = extract_keywords(pm.question)
        pm_norm = normalize_text(pm.question)

        if not pm_keywords:
            continue

        best_match = None
        best_score = 0.0
        best_method = ""

        # Pre-filter: require at least 1 keyword overlap
        for kd in kalshi_data:
            if kd["market"].ticker in used_kalshi:
                continue

            overlap = pm_keywords & kd["keywords"]
            if not overlap:
                continue

            # Full fuzzy matching only on candidates with keyword overlap
            title_score = fuzz.token_sort_ratio(pm_norm, kd["norm_title"])
            combined_score = fuzz.token_sort_ratio(pm_norm, kd["norm_combined"])
            score = max(title_score, combined_score)
            method = "title" if title_score >= combined_score else "combined"

            if score > best_score:
                best_score = score
                best_match = kd["market"]
                best_method = method

        if best_match and best_score >= threshold:
            matches.append(MatchedMarket(
                polymarket=pm,
                kalshi=best_match,
                match_score=best_score,
                match_method=best_method,
            ))
            used_kalshi.add(best_match.ticker)
            logger.debug(
                "Matched (%.1f%% via %s): '%s' <-> '%s'",
                best_score, best_method, pm.question[:60], best_match.title[:60],
            )

    logger.info("Found %d market matches (threshold=%d)", len(matches), threshold)
    return matches

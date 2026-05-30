# -*- coding: utf-8 -*-
"""Stock universe loading for full-market recommendation screening."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional

from src.data.stock_index_loader import find_existing_stock_index_path
from src.services.recommendation_models import StockUniverseItem


def infer_cn_board(code: str, name: str = "") -> str:
    """Infer an A-share board from code/name for rule defaults."""
    normalized = str(code or "").strip().upper()
    if "." in normalized:
        normalized = normalized.split(".", 1)[0]
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if "ST" in str(name or "").upper():
        return "st"
    if digits.startswith(("8", "4", "92")):
        return "bse"
    if digits.startswith("30"):
        return "chinext"
    if digits.startswith("68"):
        return "star"
    if digits.startswith(("60", "00", "001", "002", "003")):
        return "main"
    return "unknown"


def _load_index_payload(index_path: Path) -> list:
    with index_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, list):
        raise ValueError(f"股票索引格式无效: {index_path}")
    return payload


def load_stock_universe(
    *,
    market: str = "cn",
    asset_types: Optional[Iterable[str]] = None,
    index_path: Optional[Path] = None,
) -> List[StockUniverseItem]:
    """Load active stock universe from the generated stocks.index.json."""
    effective_market = (market or "cn").strip().lower()
    stock_market = "CN" if effective_market == "cn" else effective_market.upper()
    allowed_asset_types = {str(item).strip().lower() for item in (asset_types or ["stock"]) if str(item).strip()}

    path = index_path or find_existing_stock_index_path()
    if path is None:
        raise FileNotFoundError("未找到 stocks.index.json，无法加载全市场股票池")

    universe: List[StockUniverseItem] = []
    for item in _load_index_payload(path):
        if not isinstance(item, list) or len(item) < 10:
            continue
        canonical_code, display_code, name, _pinyin, _abbr, _aliases, item_market, asset_type, active, *_rest = item
        item_market_text = str(item_market or "").strip().upper()
        asset_type_text = str(asset_type or "").strip().lower()
        if item_market_text != stock_market:
            continue
        if allowed_asset_types and asset_type_text not in allowed_asset_types:
            continue
        if active is not True:
            continue
        code = str(canonical_code or display_code or "").strip().upper()
        display = str(display_code or canonical_code or "").strip().upper()
        stock_name = str(name or "").strip()
        if not code or not stock_name:
            continue
        universe.append(
            StockUniverseItem(
                code=code,
                display_code=display,
                name=stock_name,
                market=item_market_text,
                asset_type=asset_type_text,
                active=True,
                board=infer_cn_board(display or code, stock_name),
            )
        )

    return universe

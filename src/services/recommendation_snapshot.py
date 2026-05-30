# -*- coding: utf-8 -*-
"""Full-market quote snapshot fetching and normalization."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from data_provider.realtime_types import safe_float, safe_int
from src.services.recommendation_models import MarketSnapshotItem, StockUniverseItem

logger = logging.getLogger(__name__)


def _first_existing(row: Any, columns: Iterable[str]) -> Any:
    for col in columns:
        try:
            value = row.get(col)
        except AttributeError:
            value = None
        if value is not None:
            return value
    return None


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.endswith((".SH", ".SZ", ".SS", ".BJ")):
        text = text.rsplit(".", 1)[0]
    if text.startswith(("SH", "SZ", "BJ")) and text[2:].isdigit():
        text = text[2:]
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


class MarketSnapshotFetcher:
    """Fetch A-share full-market snapshots from configured providers."""

    def __init__(
        self,
        source_fetchers: Optional[Dict[str, Callable[[], pd.DataFrame]]] = None,
    ):
        self._source_fetchers = source_fetchers or {
            "efinance": self._fetch_efinance,
            "akshare_em": self._fetch_akshare_em,
            "tencent": self._fetch_tencent,
            "akshare_sina": self._fetch_akshare_sina,
        }

    def fetch(
        self,
        *,
        trade_date: date,
        universe: List[StockUniverseItem],
        preferred_sources: List[str],
    ) -> Tuple[List[MarketSnapshotItem], List[Dict[str, Any]]]:
        """Fetch and normalize a full-market snapshot."""
        attempts: List[Dict[str, Any]] = []
        universe_codes = {_normalize_code(item.display_code or item.code): item for item in universe}
        for source in preferred_sources:
            fetcher = self._source_fetchers.get(source)
            if fetcher is None:
                attempts.append({"name": source, "status": "unsupported"})
                continue
            try:
                df = fetcher(universe) if source == "tencent" else fetcher()
                if df is None or df.empty:
                    attempts.append({"name": source, "status": "empty"})
                    continue
                items = self._normalize_dataframe(
                    df,
                    source=source,
                    trade_date=trade_date,
                    universe_codes=universe_codes,
                )
                if items:
                    attempts.append({"name": source, "status": "ok", "rows": len(items)})
                    return items, attempts
                attempts.append({"name": source, "status": "no_universe_match", "rows": len(df)})
            except Exception as exc:  # noqa: BLE001 - provider failures are expected.
                logger.warning("[recommendation] snapshot provider %s failed: %s", source, exc)
                attempts.append({"name": source, "status": "failed", "error": str(exc)})
        return [], attempts

    @staticmethod
    def _fetch_efinance() -> pd.DataFrame:
        import efinance as ef

        return ef.stock.get_realtime_quotes()

    @staticmethod
    def _fetch_akshare_em() -> pd.DataFrame:
        import akshare as ak

        return ak.stock_zh_a_spot_em()

    @staticmethod
    def _fetch_akshare_sina() -> pd.DataFrame:
        import akshare as ak

        return ak.stock_zh_a_spot()

    @staticmethod
    def _fetch_tencent(universe: List[StockUniverseItem]) -> pd.DataFrame:
        import requests

        def symbol_for(item: StockUniverseItem) -> Optional[str]:
            code = _normalize_code(item.display_code or item.code)
            if not code.isdigit() or len(code) != 6:
                return None
            if code.startswith(("600", "601", "603", "605", "688")):
                return f"sh{code}"
            if code.startswith(("000", "001", "002", "003", "300", "301")):
                return f"sz{code}"
            return None

        def at(fields: List[str], index: int) -> Any:
            return fields[index] if len(fields) > index else None

        rows: List[Dict[str, Any]] = []
        symbols = [symbol for item in universe if (symbol := symbol_for(item))]
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        for start in range(0, len(symbols), 200):
            batch = symbols[start:start + 200]
            if not batch:
                continue
            response = session.get(
                "https://qt.gtimg.cn/q=" + ",".join(batch),
                timeout=10,
            )
            response.raise_for_status()
            for line in response.text.splitlines():
                if '="' not in line:
                    continue
                payload = line.split('="', 1)[1].rsplit('"', 1)[0]
                fields = payload.split("~")
                code = _normalize_code(at(fields, 2))
                if not code:
                    continue
                rows.append({
                    "代码": code,
                    "名称": at(fields, 1),
                    "最新价": at(fields, 3),
                    "昨收": at(fields, 4),
                    "今开": at(fields, 5),
                    "成交量": at(fields, 36),
                    "成交额": safe_float(at(fields, 37)) * 10000 if safe_float(at(fields, 37)) is not None else None,
                    "涨跌额": at(fields, 31),
                    "涨跌幅": at(fields, 32),
                    "最高": at(fields, 33),
                    "最低": at(fields, 34),
                    "换手率": at(fields, 38),
                    "市盈率": at(fields, 39),
                    "振幅": at(fields, 43),
                    "总市值": safe_float(at(fields, 44)) * 100000000 if safe_float(at(fields, 44)) is not None else None,
                    "流通市值": safe_float(at(fields, 45)) * 100000000 if safe_float(at(fields, 45)) is not None else None,
                })
        return pd.DataFrame(rows)

    def _normalize_dataframe(
        self,
        df: pd.DataFrame,
        *,
        source: str,
        trade_date: date,
        universe_codes: Dict[str, StockUniverseItem],
    ) -> List[MarketSnapshotItem]:
        code_cols = ("股票代码", "代码", "code", "股票代码")
        name_cols = ("股票名称", "名称", "name")
        output: List[MarketSnapshotItem] = []
        for _, row in df.iterrows():
            code = _normalize_code(_first_existing(row, code_cols))
            if not code or code not in universe_codes:
                continue
            universe_item = universe_codes[code]
            name = str(_first_existing(row, name_cols) or universe_item.name or "").strip()
            price = safe_float(_first_existing(row, ("最新价", "price", "最新")))
            volume = safe_int(_first_existing(row, ("成交量", "volume")))
            amount = safe_float(_first_existing(row, ("成交额", "amount")))
            quality = "ok" if price and price > 0 else "unavailable"
            output.append(
                MarketSnapshotItem(
                    trade_date=trade_date,
                    code=code,
                    name=name,
                    price=price,
                    change_pct=safe_float(_first_existing(row, ("涨跌幅", "pct_chg", "change_pct"))),
                    change_amount=safe_float(_first_existing(row, ("涨跌额", "change", "change_amount"))),
                    volume=volume,
                    amount=amount,
                    volume_ratio=safe_float(_first_existing(row, ("量比", "volume_ratio"))),
                    turnover_rate=safe_float(_first_existing(row, ("换手率", "turnover_rate"))),
                    amplitude=safe_float(_first_existing(row, ("振幅", "amplitude"))),
                    open=safe_float(_first_existing(row, ("今开", "开盘", "open"))),
                    high=safe_float(_first_existing(row, ("最高", "high"))),
                    low=safe_float(_first_existing(row, ("最低", "low"))),
                    pre_close=safe_float(_first_existing(row, ("昨收", "pre_close"))),
                    pe_ratio=safe_float(_first_existing(row, ("市盈率-动态", "市盈率", "pe_ratio"))),
                    pb_ratio=safe_float(_first_existing(row, ("市净率", "pb_ratio"))),
                    total_mv=safe_float(_first_existing(row, ("总市值", "total_mv"))),
                    circ_mv=safe_float(_first_existing(row, ("流通市值", "circ_mv"))),
                    change_60d=safe_float(_first_existing(row, ("60日涨跌幅", "change_60d"))),
                    high_52w=safe_float(_first_existing(row, ("52周最高", "high_52w"))),
                    low_52w=safe_float(_first_existing(row, ("52周最低", "low_52w"))),
                    source=source,
                    data_quality=quality,
                )
            )
        return output

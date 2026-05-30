# -*- coding: utf-8 -*-
"""Rule-based full-market recommendation screening."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from data_provider import DataFetcherManager
from src.services.recommendation_models import (
    CandidateScore,
    MarketSnapshotItem,
    ScreeningFeatures,
    StockUniverseItem,
)
from src.services.recommendation_profile import RecommendationProfile

logger = logging.getLogger(__name__)


HistoryLoader = Callable[[str, int], Tuple[Optional[pd.DataFrame], str]]


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _score_band(value: Optional[float], low: float, high: float, *, ideal_low: Optional[float] = None, ideal_high: Optional[float] = None) -> float:
    """Return 0..1 score for value inside [low, high], best in ideal range."""
    if value is None:
        return 0.0
    if value < low or value > high:
        return 0.0
    ideal_low = low if ideal_low is None else ideal_low
    ideal_high = high if ideal_high is None else ideal_high
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        span = max(ideal_low - low, 1e-9)
        return max(0.0, min(1.0, (value - low) / span))
    span = max(high - ideal_high, 1e-9)
    return max(0.0, min(1.0, (high - value) / span))


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base == 0:
        return None
    return (current - base) / base * 100


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _rolling_return(close: pd.Series, window: int) -> Optional[float]:
    if len(close) <= window:
        return None
    base = _num(close.iloc[-window - 1])
    current = _num(close.iloc[-1])
    return _pct_change(current, base)


def _max_drawdown(close: pd.Series, window: int) -> Optional[float]:
    if len(close) < 2:
        return None
    series = close.tail(window).astype(float)
    running_max = series.cummax()
    drawdowns = (series - running_max) / running_max * 100
    return abs(float(drawdowns.min()))


def _consecutive_days(close: pd.Series, *, up: bool) -> int:
    count = 0
    values = close.astype(float).tolist()
    for idx in range(len(values) - 1, 0, -1):
        if up and values[idx] > values[idx - 1]:
            count += 1
        elif not up and values[idx] < values[idx - 1]:
            count += 1
        else:
            break
    return count


def _rsi(close: pd.Series, period: int) -> Optional[float]:
    if len(close) <= period:
        return None
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    latest_loss = _num(loss.iloc[-1])
    latest_gain = _num(gain.iloc[-1])
    if latest_gain is None or latest_loss is None:
        return None
    if latest_loss == 0:
        return 100.0
    rs = latest_gain / latest_loss
    return 100 - (100 / (1 + rs))


def _find_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    lower_map = {str(col).lower(): col for col in df.columns}
    for candidate in candidates:
        match = lower_map.get(candidate.lower())
        if match is not None:
            return str(match)
    return None


@dataclass
class ScreeningResult:
    candidates: List[CandidateScore]
    recommendations: List[CandidateScore]
    filtered_count: int
    history_loaded_count: int
    warnings: List[str]
    filter_breakdown: Dict[str, int]


class RecommendationScreener:
    """Apply hard filters, feature calculations and scoring to snapshot data."""

    def __init__(
        self,
        profile: RecommendationProfile,
        history_loader: Optional[HistoryLoader] = None,
    ):
        self.profile = profile
        self._history_loader = history_loader or self._default_history_loader

    @staticmethod
    def _default_history_loader(code: str, days: int) -> Tuple[Optional[pd.DataFrame], str]:
        df = RecommendationScreener._tencent_history_loader(code, days)
        if df is not None and not df.empty:
            return df, "tencent"
        manager = DataFetcherManager()
        return manager.get_daily_data(code, days=days)

    @staticmethod
    def _tencent_history_loader(code: str, days: int) -> Optional[pd.DataFrame]:
        import requests

        symbol = RecommendationScreener._tencent_symbol(code)
        if symbol is None:
            return None
        response = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,,{max(days, 60)},qfq"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        node = payload.get("data", {}).get(symbol, {})
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
        for column in ("open", "close", "high", "low", "volume"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["amount"] = df["close"] * df["volume"] * 100
        return df.dropna(subset=["close"]).tail(days).reset_index(drop=True)

    @staticmethod
    def _tencent_symbol(code: str) -> Optional[str]:
        plain = RecommendationScreener._plain_code(code)
        if not plain.isdigit() or len(plain) != 6:
            return None
        if plain.startswith(("600", "601", "603", "605", "688")):
            return f"sh{plain}"
        if plain.startswith(("000", "001", "002", "003", "300", "301")):
            return f"sz{plain}"
        return None

    def screen(
        self,
        *,
        trade_date: date,
        universe: List[StockUniverseItem],
        snapshot: List[MarketSnapshotItem],
    ) -> ScreeningResult:
        universe_by_code = {self._plain_code(item.display_code or item.code): item for item in universe}
        snapshot_by_code = {self._plain_code(item.code): item for item in snapshot}
        filtered_count = 0
        filter_breakdown: Dict[str, int] = {}
        fast_candidates: List[Tuple[MarketSnapshotItem, StockUniverseItem, CandidateScore, float]] = []

        for code, quote in snapshot_by_code.items():
            universe_item = universe_by_code.get(code)
            if universe_item is None:
                continue
            candidate = CandidateScore(
                trade_date=trade_date,
                code=code,
                name=quote.name or universe_item.name,
            )
            self._apply_hard_filters(candidate, quote, universe_item)
            if not candidate.passed_hard_filters:
                filtered_count += 1
                self._record_filter_reasons(filter_breakdown, candidate)
                continue
            fast_score = self._fast_prefilter_score(quote, universe_item)
            fast_candidates.append((quote, universe_item, candidate, fast_score))

        fast_candidates.sort(key=lambda item: item[3], reverse=True)
        history_limit = self.profile.history_prefilter_limit()
        selected_for_history = fast_candidates[:history_limit]

        warnings: List[str] = []
        scored: List[CandidateScore] = []
        history_loaded_count = 0
        for quote, universe_item, candidate, _fast_score in selected_for_history:
            features = self._load_features(candidate.code)
            if features is None:
                candidate.passed_hard_filters = False
                candidate.filtered_by.append("history_unavailable")
                filtered_count += 1
                self._record_filter_reasons(filter_breakdown, candidate)
                continue
            history_loaded_count += 1
            self._apply_feature_hard_filters(candidate, quote, features)
            if not candidate.passed_hard_filters:
                filtered_count += 1
                self._record_filter_reasons(filter_breakdown, candidate)
                continue
            self._score_candidate(candidate, quote, universe_item, features)
            scored.append(candidate)

        scored.sort(key=lambda item: item.selection_score, reverse=True)
        candidates = scored[: self.profile.candidate_limit()]
        for idx, candidate in enumerate(candidates, start=1):
            candidate.rank = idx

        recommendations = self._select_recommendations(candidates)
        return ScreeningResult(
            candidates=candidates,
            recommendations=recommendations,
            filtered_count=filtered_count,
            history_loaded_count=history_loaded_count,
            warnings=warnings,
            filter_breakdown=dict(sorted(filter_breakdown.items())),
        )

    @staticmethod
    def _record_filter_reasons(breakdown: Dict[str, int], candidate: CandidateScore) -> None:
        reasons = candidate.filtered_by or ["unknown"]
        for reason in reasons:
            breakdown[reason] = breakdown.get(reason, 0) + 1

    @staticmethod
    def _plain_code(code: str) -> str:
        text = str(code or "").strip().upper()
        if "." in text:
            text = text.rsplit(".", 1)[0]
        if text.startswith(("SH", "SZ", "BJ")) and text[2:].isdigit():
            text = text[2:]
        if text.isdigit() and len(text) <= 6:
            return text.zfill(6)
        return text

    def _section(self, *keys: str) -> Dict[str, Any]:
        current: Any = self.profile.raw
        for key in keys:
            if not isinstance(current, dict):
                return {}
            current = current.get(key, {})
        return current if isinstance(current, dict) else {}

    def _apply_hard_filters(
        self,
        candidate: CandidateScore,
        quote: MarketSnapshotItem,
        universe_item: StockUniverseItem,
    ) -> None:
        universe_cfg = self._section("universe")
        price_cfg = self._section("hard_filters", "price")
        liquidity_cfg = self._section("hard_filters", "liquidity")
        intraday_cfg = self._section("hard_filters", "intraday")
        name_upper = (quote.name or universe_item.name or "").upper()
        board = (universe_item.board or "unknown").lower()

        if universe_cfg.get("exclude_st", True) and "ST" in name_upper:
            candidate.filtered_by.append("st_or_special_treatment")
        if universe_cfg.get("exclude_delisting_risk", True) and "退" in (quote.name or universe_item.name):
            candidate.filtered_by.append("delisting_risk")
        if universe_cfg.get("exclude_bse", True) and board == "bse":
            candidate.filtered_by.append("bse_excluded")
        self._apply_listing_age_filter(candidate, universe_item)

        price = _num(quote.price)
        if price is None or price <= 0:
            candidate.filtered_by.append("invalid_price")
        else:
            min_price = float(price_cfg.get("min_price", 0) or 0)
            max_price = float(price_cfg.get("max_price", 10**9) or 10**9)
            if price < min_price:
                candidate.filtered_by.append("price_too_low")
            if price > max_price:
                candidate.filtered_by.append("price_too_high")

        if (quote.volume is None or quote.volume <= 0) or (quote.amount is None or quote.amount <= 0):
            candidate.filtered_by.append("suspended_or_no_liquidity")
        min_amount = float(liquidity_cfg.get("min_amount", 0) or 0)
        if quote.amount is not None and quote.amount < min_amount:
            candidate.filtered_by.append("amount_too_low")

        max_abs_change_pct = float(intraday_cfg.get("max_abs_change_pct", 100) or 100)
        if quote.change_pct is not None and abs(quote.change_pct) > max_abs_change_pct:
            candidate.filtered_by.append("change_pct_extreme")
        max_amplitude = float(intraday_cfg.get("max_amplitude", 100) or 100)
        if quote.amplitude is not None and quote.amplitude > max_amplitude:
            candidate.filtered_by.append("amplitude_extreme")
        max_volume_ratio = float(intraday_cfg.get("max_volume_ratio", 10**9) or 10**9)
        if quote.volume_ratio is not None and quote.volume_ratio > max_volume_ratio:
            candidate.filtered_by.append("volume_ratio_extreme")
        max_turnover = float(intraday_cfg.get("max_turnover_rate", 10**9) or 10**9)
        if quote.turnover_rate is not None and quote.turnover_rate > max_turnover:
            candidate.filtered_by.append("turnover_extreme")

        if intraday_cfg.get("exclude_limit_like", True) and self._is_limit_like(quote, board):
            candidate.filtered_by.append("limit_like")
        if intraday_cfg.get("exclude_one_word_board", True) and self._is_one_word_board(quote, board):
            candidate.filtered_by.append("one_word_board")

        if candidate.filtered_by:
            candidate.passed_hard_filters = False

    def _apply_listing_age_filter(self, candidate: CandidateScore, universe_item: StockUniverseItem) -> None:
        min_age_days = int(self._section("universe").get("exclude_new_stock_days", 0) or 0)
        if min_age_days <= 0:
            return
        if not universe_item.listing_date:
            candidate.risk_tags.append("listing_age_unknown")
            return
        try:
            listed_at = datetime.strptime(str(universe_item.listing_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            candidate.risk_tags.append("listing_age_unknown")
            return
        if (candidate.trade_date - listed_at).days < min_age_days:
            candidate.filtered_by.append("new_stock")

    def _apply_feature_hard_filters(
        self,
        candidate: CandidateScore,
        quote: MarketSnapshotItem,
        features: ScreeningFeatures,
    ) -> None:
        liquidity_cfg = self._section("hard_filters", "liquidity")
        min_avg_amount_5d = float(liquidity_cfg.get("min_avg_amount_5d", 0) or 0)
        min_avg_amount_20d = float(liquidity_cfg.get("min_avg_amount_20d", 0) or 0)
        min_circ_mv = float(liquidity_cfg.get("min_circ_mv", 0) or 0)

        if min_avg_amount_5d and features.avg_amount_5d is not None and features.avg_amount_5d < min_avg_amount_5d:
            candidate.filtered_by.append("avg_amount_5d_too_low")
        if min_avg_amount_20d and features.avg_amount_20d is not None and features.avg_amount_20d < min_avg_amount_20d:
            candidate.filtered_by.append("avg_amount_20d_too_low")
        if min_circ_mv and quote.circ_mv is not None and quote.circ_mv < min_circ_mv:
            candidate.filtered_by.append("circ_mv_too_low")

        if candidate.filtered_by:
            candidate.passed_hard_filters = False

    @staticmethod
    def _limit_pct_for_board(board: str) -> float:
        if board == "st":
            return 5.0
        if board in {"chinext", "star"}:
            return 20.0
        if board == "bse":
            return 30.0
        return 10.0

    def _is_limit_like(self, quote: MarketSnapshotItem, board: str) -> bool:
        change_pct = _num(quote.change_pct)
        if change_pct is None:
            return False
        return abs(change_pct) >= self._limit_pct_for_board(board) - 0.25

    def _is_one_word_board(self, quote: MarketSnapshotItem, board: str) -> bool:
        if not self._is_limit_like(quote, board):
            return False
        values = [_num(quote.open), _num(quote.high), _num(quote.low), _num(quote.price)]
        if any(value is None for value in values):
            return False
        return max(values) - min(values) <= 1e-6

    def _fast_prefilter_score(self, quote: MarketSnapshotItem, universe_item: StockUniverseItem) -> float:
        amount = _num(quote.amount) or 0.0
        amount_score = min(math.log10(max(amount, 1)) / 10, 1.0)
        change_score = _score_band(_num(quote.change_pct), -4, 6, ideal_low=-1, ideal_high=3)
        volume_score = _score_band(_num(quote.volume_ratio), 0.5, 3.5, ideal_low=0.8, ideal_high=2.0)
        mv_score = min(math.log10(max(_num(quote.circ_mv) or amount, 1)) / 12, 1.0)
        return amount_score * 35 + change_score * 25 + volume_score * 20 + mv_score * 20

    def _load_features(self, code: str) -> Optional[ScreeningFeatures]:
        try:
            df, _source = self._history_loader(code, 120)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[recommendation] history load failed for %s: %s", code, exc)
            return None
        if df is None or getattr(df, "empty", True):
            return None
        try:
            return self._compute_features(code, df)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[recommendation] feature compute failed for %s: %s", code, exc)
            return None

    def _compute_features(self, code: str, df: pd.DataFrame) -> Optional[ScreeningFeatures]:
        close_col = _find_column(df, ["close", "收盘", "收盘价"])
        high_col = _find_column(df, ["high", "最高", "最高价"])
        low_col = _find_column(df, ["low", "最低", "最低价"])
        volume_col = _find_column(df, ["volume", "成交量"])
        amount_col = _find_column(df, ["amount", "成交额"])
        if close_col is None or len(df) < 60:
            return None

        normalized = pd.DataFrame()
        normalized["close"] = pd.to_numeric(df[close_col], errors="coerce")
        normalized["high"] = pd.to_numeric(df[high_col], errors="coerce") if high_col else normalized["close"]
        normalized["low"] = pd.to_numeric(df[low_col], errors="coerce") if low_col else normalized["close"]
        normalized["volume"] = pd.to_numeric(df[volume_col], errors="coerce") if volume_col else None
        normalized["amount"] = pd.to_numeric(df[amount_col], errors="coerce") if amount_col else None
        normalized = normalized.dropna(subset=["close"]).reset_index(drop=True)
        if len(normalized) < 60:
            return None

        close = normalized["close"]
        high = normalized["high"]
        low = normalized["low"]
        current = _num(close.iloc[-1])
        ma5 = _num(close.rolling(5).mean().iloc[-1])
        ma10 = _num(close.rolling(10).mean().iloc[-1])
        ma20_series = close.rolling(20).mean()
        ma60_series = close.rolling(60).mean()
        ma20 = _num(ma20_series.iloc[-1])
        ma60 = _num(ma60_series.iloc[-1])

        prev_close = close.shift(1)
        true_range = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = _num(true_range.rolling(14).mean().iloc[-1])

        features = ScreeningFeatures(
            code=code,
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            ma60=ma60,
            bias_ma5=_pct_change(current, ma5),
            bias_ma10=_pct_change(current, ma10),
            bias_ma20=_pct_change(current, ma20),
            ma20_slope_5d=_pct_change(ma20, _num(ma20_series.iloc[-6]) if len(ma20_series) >= 6 else None),
            ma60_slope_10d=_pct_change(ma60, _num(ma60_series.iloc[-11]) if len(ma60_series) >= 11 else None),
            return_3d=_rolling_return(close, 3),
            return_5d=_rolling_return(close, 5),
            return_20d=_rolling_return(close, 20),
            return_60d=_rolling_return(close, 60),
            max_drawdown_10d=_max_drawdown(close, 10),
            max_drawdown_20d=_max_drawdown(close, 20),
            atr_pct_14d=_pct_change((current or 0) + (atr or 0), current) if current else None,
            up_day_ratio_20d=float((close.diff().tail(20) > 0).sum()) / 20,
            consecutive_up_days=_consecutive_days(close, up=True),
            consecutive_down_days=_consecutive_days(close, up=False),
            distance_to_20d_high_pct=_pct_change(current, _num(close.tail(20).max())),
            distance_to_60d_high_pct=_pct_change(current, _num(close.tail(60).max())),
            distance_to_20d_low_pct=_pct_change(current, _num(close.tail(20).min())),
            rsi_6=_rsi(close, 6),
            rsi_12=_rsi(close, 12),
            rsi_24=_rsi(close, 24),
        )
        if volume_col:
            avg_vol_5 = _num(normalized["volume"].tail(6).head(5).mean())
            latest_vol = _num(normalized["volume"].iloc[-1])
            features.volume_ratio_5d = latest_vol / avg_vol_5 if latest_vol is not None and avg_vol_5 else None
        if amount_col:
            features.avg_amount_5d = _num(normalized["amount"].tail(5).mean())
            features.avg_amount_20d = _num(normalized["amount"].tail(20).mean())
        return features

    def _score_candidate(
        self,
        candidate: CandidateScore,
        quote: MarketSnapshotItem,
        universe_item: StockUniverseItem,
        features: ScreeningFeatures,
    ) -> None:
        strategies = {
            "trend_pullback": self._strategy_trend_pullback(quote, features),
            "volume_breakout": self._strategy_volume_breakout(quote, features),
            "strong_consolidation": self._strategy_strong_consolidation(quote, features),
            "low_reversal": self._strategy_low_reversal(quote, features),
        }
        enabled_scores = {
            name: score for name, score in strategies.items()
            if self.profile.strategy_enabled(name)
        }
        candidate.strategy = max(enabled_scores, key=enabled_scores.get) if enabled_scores else "trend_pullback"

        candidate.liquidity_score = self._liquidity_score(quote, features)
        candidate.trend_score = self._trend_score(features)
        candidate.entry_quality_score = self._entry_quality_score(quote, features)
        candidate.volume_price_score = self._volume_price_score(quote, features, candidate.strategy)
        candidate.sector_score = 0.5
        candidate.stability_score = self._stability_score(features)
        candidate.risk_penalty = self._risk_penalty(candidate, quote, features)
        weights = self.profile.weights()
        total_weight = sum(weights.values()) or 1.0
        raw_score = (
            candidate.liquidity_score * weights["liquidity"]
            + candidate.trend_score * weights["trend"]
            + candidate.entry_quality_score * weights["entry_quality"]
            + candidate.volume_price_score * weights["volume_price"]
            + candidate.sector_score * weights["sector_strength"]
            + candidate.stability_score * weights["stability"]
        ) / total_weight * 100
        strategy_bonus = enabled_scores.get(candidate.strategy, 0.0) * 8
        candidate.selection_score = max(0.0, raw_score + strategy_bonus - candidate.risk_penalty)
        candidate.beginner_safety_score = max(0.0, (candidate.entry_quality_score * 0.5 + candidate.stability_score * 0.3 + candidate.liquidity_score * 0.2) * 100 - candidate.risk_penalty)
        self._add_explanations(candidate, quote, features)
        self._set_price_plan(candidate, quote, features)

    def _strategy_trend_pullback(self, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        score = 0.0
        if f.ma5 and f.ma10 and f.ma20 and f.ma5 > f.ma10 > f.ma20:
            score += 0.35
        if (f.ma20_slope_5d or 0) > 0:
            score += 0.2
        score += _score_band(f.bias_ma5, -3, 4, ideal_low=-1.5, ideal_high=2.0) * 0.25
        score += _score_band(quote.change_pct, -3, 3, ideal_low=-1.5, ideal_high=1.5) * 0.1
        score += _score_band(f.volume_ratio_5d or quote.volume_ratio, 0.5, 1.4, ideal_low=0.6, ideal_high=1.1) * 0.1
        return _clamp(score)

    def _strategy_volume_breakout(self, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        score = _score_band(quote.change_pct, 2, 6, ideal_low=2.5, ideal_high=5.0) * 0.35
        score += _score_band(quote.volume_ratio or f.volume_ratio_5d, 1.3, 3.8, ideal_low=1.5, ideal_high=3.0) * 0.3
        if f.ma20 and quote.price and quote.price > f.ma20:
            score += 0.2
        if f.ma5 and f.ma10 and f.ma5 >= f.ma10 * 0.99:
            score += 0.15
        return _clamp(score)

    def _strategy_strong_consolidation(self, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        score = _score_band(f.return_60d, 5, 60, ideal_low=12, ideal_high=40) * 0.3
        score += _score_band(f.return_5d, -6, 6, ideal_low=-3, ideal_high=3) * 0.25
        if f.ma20 and quote.price and quote.price >= f.ma20:
            score += 0.2
        score += _score_band(f.volume_ratio_5d or quote.volume_ratio, 0.4, 1.2, ideal_low=0.5, ideal_high=0.9) * 0.15
        if (f.max_drawdown_20d or 100) <= 12:
            score += 0.1
        return _clamp(score)

    def _strategy_low_reversal(self, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        score = 0.0
        if f.ma20 and quote.price and quote.price > f.ma20:
            score += 0.3
        if (f.ma20_slope_5d or -1) > 0:
            score += 0.2
        score += _score_band(quote.volume_ratio or f.volume_ratio_5d, 1.1, 3.2, ideal_low=1.3, ideal_high=2.6) * 0.25
        score += _score_band(f.return_20d, -8, 15, ideal_low=0, ideal_high=10) * 0.25
        return _clamp(score)

    def _liquidity_score(self, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        amount = max(_num(quote.amount) or 0, _num(f.avg_amount_20d) or 0)
        circ_mv = _num(quote.circ_mv) or 0
        amount_score = _clamp((math.log10(max(amount, 1)) - 8) / 2)
        mv_score = _clamp((math.log10(max(circ_mv, 1)) - 9.5) / 2.0) if circ_mv else 0.5
        return amount_score * 0.75 + mv_score * 0.25

    def _trend_score(self, f: ScreeningFeatures) -> float:
        score = 0.0
        if f.ma5 and f.ma10 and f.ma20 and f.ma5 > f.ma10 > f.ma20:
            score += 0.35
        if f.ma20 and f.ma60 and f.ma20 > f.ma60:
            score += 0.2
        if (f.ma20_slope_5d or 0) > 0:
            score += 0.15
        score += _score_band(f.return_20d, -5, 25, ideal_low=2, ideal_high=15) * 0.15
        score += _score_band(f.return_60d, -10, 60, ideal_low=5, ideal_high=35) * 0.15
        return _clamp(score)

    def _entry_quality_score(self, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        score = _score_band(f.bias_ma5, -3, 5, ideal_low=-1, ideal_high=2.5) * 0.35
        score += _score_band(f.bias_ma20, -2, 10, ideal_low=0, ideal_high=6) * 0.25
        score += _score_band(quote.change_pct, -3, 4, ideal_low=-1, ideal_high=2) * 0.2
        score += _score_band(abs(f.distance_to_20d_high_pct or 0), 0, 18, ideal_low=4, ideal_high=14) * 0.1
        stop_distance = abs(_pct_change((quote.price or 0) * 0.96, quote.price) or 0)
        score += _score_band(stop_distance, 2, 8, ideal_low=3, ideal_high=6) * 0.1
        return _clamp(score)

    def _volume_price_score(self, quote: MarketSnapshotItem, f: ScreeningFeatures, strategy: str) -> float:
        ratio = quote.volume_ratio if quote.volume_ratio is not None else f.volume_ratio_5d
        if strategy == "volume_breakout":
            return _score_band(ratio, 1.2, 3.8, ideal_low=1.5, ideal_high=3.0)
        return _score_band(ratio, 0.4, 1.8, ideal_low=0.6, ideal_high=1.2)

    def _stability_score(self, f: ScreeningFeatures) -> float:
        score = _score_band(f.atr_pct_14d, 0.5, 8, ideal_low=1, ideal_high=4) * 0.3
        score += _score_band(f.max_drawdown_20d, 0, 18, ideal_low=2, ideal_high=10) * 0.35
        score += _score_band(f.up_day_ratio_20d, 0.35, 0.75, ideal_low=0.45, ideal_high=0.65) * 0.2
        score += _score_band(float(f.consecutive_up_days), 0, 5, ideal_low=0, ideal_high=3) * 0.15
        return _clamp(score)

    def _risk_penalty(self, candidate: CandidateScore, quote: MarketSnapshotItem, f: ScreeningFeatures) -> float:
        penalty = 0.0
        if f.bias_ma5 is not None and f.bias_ma5 > 8:
            candidate.risk_tags.append("overextended")
            penalty += 12
        if quote.turnover_rate is not None and quote.turnover_rate > 15:
            candidate.risk_tags.append("high_turnover")
            penalty += 8
        if quote.volume_ratio is not None and quote.volume_ratio > 4:
            candidate.risk_tags.append("volume_spike")
            penalty += 8
        if f.distance_to_20d_high_pct is not None and abs(f.distance_to_20d_high_pct) < 2:
            candidate.risk_tags.append("near_resistance")
            penalty += 5
        if f.max_drawdown_20d is not None and f.max_drawdown_20d > 18:
            candidate.risk_tags.append("recent_drawdown")
            penalty += 10
        if f.ma20 and quote.price and quote.price < f.ma20:
            candidate.risk_tags.append("trend_broken")
            penalty += 15
        candidate.risk_tags.append("sector_data_unavailable")
        return penalty

    def _add_explanations(self, candidate: CandidateScore, quote: MarketSnapshotItem, f: ScreeningFeatures) -> None:
        if f.ma5 and f.ma10 and f.ma20 and f.ma5 > f.ma10 > f.ma20:
            candidate.positive_reasons.append("MA5>MA10>MA20 多头排列")
        if (f.ma20_slope_5d or 0) > 0:
            candidate.positive_reasons.append("MA20 斜率向上")
        if candidate.entry_quality_score >= 0.65:
            candidate.positive_reasons.append("买点距离短期均线较近")
        if candidate.liquidity_score >= 0.65:
            candidate.positive_reasons.append("成交额和流动性较充足")
        if "sector_data_unavailable" in candidate.risk_tags:
            candidate.negative_reasons.append("板块强度数据暂不可用，按中性处理")
        if candidate.risk_penalty > 0:
            candidate.negative_reasons.append("存在风险标签: " + ";".join(tag for tag in candidate.risk_tags if tag != "sector_data_unavailable"))

    def _set_price_plan(self, candidate: CandidateScore, quote: MarketSnapshotItem, f: ScreeningFeatures) -> None:
        price = _num(quote.price)
        if price is None:
            return
        support_values = [value for value in (f.ma5, f.ma10, f.ma20) if value and value > 0]
        candidate.watch_price = round(min(support_values, key=lambda value: abs(price - value)) if support_values else price, 3)
        stop_base = f.ma20 or min(support_values) if support_values else price * 0.95
        candidate.stop_loss = round(float(stop_base) * 0.97, 3)
        high_ref = price / (1 + (f.distance_to_20d_high_pct or -5) / 100) if f.distance_to_20d_high_pct is not None else price * 1.08
        candidate.take_profit_reference = round(max(price * 1.04, high_ref), 3)

    def _select_recommendations(self, candidates: List[CandidateScore]) -> List[CandidateScore]:
        min_score = self.profile.min_final_score()
        recommend_limit = self.profile.recommend_limit()
        strategy_order = ["trend_pullback", "volume_breakout", "strong_consolidation", "low_reversal"]
        selected: List[CandidateScore] = []
        seen_codes: set[str] = set()
        for strategy in strategy_order:
            cfg = self.profile.strategy_config(strategy)
            max_for_strategy = int(cfg.get("max_recommendations", 10) or 10)
            min_strategy_score = float(cfg.get("min_score", min_score) or min_score)
            taken = 0
            for candidate in candidates:
                if candidate.code in seen_codes or candidate.strategy != strategy:
                    continue
                if candidate.selection_score < max(min_score, min_strategy_score):
                    continue
                self._mark_recommendation(candidate)
                selected.append(candidate)
                seen_codes.add(candidate.code)
                taken += 1
                if len(selected) >= recommend_limit or taken >= max_for_strategy:
                    break
            if len(selected) >= recommend_limit:
                break
        if len(selected) < recommend_limit:
            for candidate in candidates:
                if candidate.code in seen_codes or candidate.selection_score < min_score:
                    continue
                self._mark_recommendation(candidate)
                selected.append(candidate)
                seen_codes.add(candidate.code)
                if len(selected) >= recommend_limit:
                    break
        for idx, candidate in enumerate(selected, start=1):
            candidate.rank = idx
        return selected

    @staticmethod
    def _mark_recommendation(candidate: CandidateScore) -> None:
        if candidate.selection_score >= 82 and candidate.beginner_safety_score >= 70:
            candidate.recommendation_level = "focus"
            candidate.recommendation_label = "重点关注"
        elif candidate.selection_score >= 74:
            candidate.recommendation_level = "confirm"
            candidate.recommendation_label = "观察确认"
        else:
            candidate.recommendation_level = "watch_only"
            candidate.recommendation_label = "只看不追"
        candidate.beginner_action = "不追高，优先等待回踩关注价附近再观察确认"
        candidate.no_position_action = "未持仓先加入观察，次日高开过多不追"
        candidate.has_position_action = "已持仓可参考止损位管理风险，跌破则降低仓位或放弃"

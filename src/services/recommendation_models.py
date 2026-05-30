# -*- coding: utf-8 -*-
"""Dataclasses used by the full-market recommendation screener."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StockUniverseItem:
    code: str
    display_code: str
    name: str
    market: str
    asset_type: str
    active: bool
    board: str = "unknown"
    industry: Optional[str] = None
    sector: Optional[str] = None
    listing_date: Optional[str] = None


@dataclass
class MarketSnapshotItem:
    trade_date: date
    code: str
    name: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    change_amount: Optional[float] = None
    volume: Optional[int] = None
    amount: Optional[float] = None
    volume_ratio: Optional[float] = None
    turnover_rate: Optional[float] = None
    amplitude: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pre_close: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    total_mv: Optional[float] = None
    circ_mv: Optional[float] = None
    change_60d: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    source: str = ""
    data_quality: str = "ok"

    def to_csv_row(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "change_pct": self.change_pct,
            "change_amount": self.change_amount,
            "volume": self.volume,
            "amount": self.amount,
            "volume_ratio": self.volume_ratio,
            "turnover_rate": self.turnover_rate,
            "amplitude": self.amplitude,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "pre_close": self.pre_close,
            "pe_ratio": self.pe_ratio,
            "pb_ratio": self.pb_ratio,
            "total_mv": self.total_mv,
            "circ_mv": self.circ_mv,
            "change_60d": self.change_60d,
            "high_52w": self.high_52w,
            "low_52w": self.low_52w,
            "source": self.source,
            "data_quality": self.data_quality,
        }


@dataclass
class ScreeningFeatures:
    code: str
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    bias_ma5: Optional[float] = None
    bias_ma10: Optional[float] = None
    bias_ma20: Optional[float] = None
    ma20_slope_5d: Optional[float] = None
    ma60_slope_10d: Optional[float] = None
    return_3d: Optional[float] = None
    return_5d: Optional[float] = None
    return_20d: Optional[float] = None
    return_60d: Optional[float] = None
    volume_ratio_5d: Optional[float] = None
    avg_amount_5d: Optional[float] = None
    avg_amount_20d: Optional[float] = None
    max_drawdown_10d: Optional[float] = None
    max_drawdown_20d: Optional[float] = None
    atr_pct_14d: Optional[float] = None
    up_day_ratio_20d: Optional[float] = None
    consecutive_up_days: int = 0
    consecutive_down_days: int = 0
    distance_to_20d_high_pct: Optional[float] = None
    distance_to_60d_high_pct: Optional[float] = None
    distance_to_20d_low_pct: Optional[float] = None
    rsi_6: Optional[float] = None
    rsi_12: Optional[float] = None
    rsi_24: Optional[float] = None


@dataclass
class CandidateScore:
    trade_date: date
    code: str
    name: str
    strategy: str = ""
    passed_hard_filters: bool = True
    filtered_by: List[str] = field(default_factory=list)
    selection_score: float = 0.0
    beginner_safety_score: float = 0.0
    entry_quality_score: float = 0.0
    liquidity_score: float = 0.0
    trend_score: float = 0.0
    volume_price_score: float = 0.0
    sector_score: float = 0.0
    stability_score: float = 0.0
    risk_penalty: float = 0.0
    risk_tags: List[str] = field(default_factory=list)
    positive_reasons: List[str] = field(default_factory=list)
    negative_reasons: List[str] = field(default_factory=list)
    watch_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_reference: Optional[float] = None
    rank: Optional[int] = None
    recommendation_level: str = ""
    recommendation_label: str = ""
    beginner_action: str = ""
    no_position_action: str = ""
    has_position_action: str = ""
    llm_review_status: str = "not_run"
    analysis_query_id: str = ""

    def to_csv_row(self, *, include_recommendation_fields: bool = False) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "trade_date": self.trade_date.isoformat(),
            "code": self.code,
            "name": self.name,
            "strategy": self.strategy,
            "passed_hard_filters": self.passed_hard_filters,
            "filtered_by": ";".join(self.filtered_by),
            "selection_score": round(self.selection_score, 4),
            "beginner_safety_score": round(self.beginner_safety_score, 4),
            "entry_quality_score": round(self.entry_quality_score, 4),
            "liquidity_score": round(self.liquidity_score, 4),
            "trend_score": round(self.trend_score, 4),
            "volume_price_score": round(self.volume_price_score, 4),
            "sector_score": round(self.sector_score, 4),
            "stability_score": round(self.stability_score, 4),
            "risk_penalty": round(self.risk_penalty, 4),
            "risk_tags": ";".join(self.risk_tags),
            "positive_reasons": ";".join(self.positive_reasons),
            "negative_reasons": ";".join(self.negative_reasons),
            "watch_price": self.watch_price,
            "stop_loss": self.stop_loss,
            "take_profit_reference": self.take_profit_reference,
            "rank": self.rank,
        }
        if include_recommendation_fields:
            row.update({
                "recommendation_level": self.recommendation_level,
                "recommendation_label": self.recommendation_label,
                "beginner_action": self.beginner_action,
                "no_position_action": self.no_position_action,
                "has_position_action": self.has_position_action,
                "llm_review_status": self.llm_review_status,
                "analysis_query_id": self.analysis_query_id,
            })
        return row


@dataclass
class RecommendationRunArtifacts:
    run_id: str
    market: str
    trade_date: date
    generated_at: datetime
    snapshot_file: Path
    candidates_file: Optional[Path]
    recommendations_file: Optional[Path]
    meta_file: Path
    profile_snapshot_file: Path
    summary: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)

    def to_response(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "market": self.market,
            "trade_date": self.trade_date.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "snapshot_file": str(self.snapshot_file),
            "candidates_file": str(self.candidates_file) if self.candidates_file else None,
            "recommendations_file": str(self.recommendations_file) if self.recommendations_file else None,
            "meta_file": str(self.meta_file),
            "profile_snapshot_file": str(self.profile_snapshot_file),
            "summary": self.summary,
            "warnings": self.warnings,
        }

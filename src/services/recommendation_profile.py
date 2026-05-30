# -*- coding: utf-8 -*-
"""Configuration profiles for the full-market recommendation screener."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECOMMENDATION_PROFILE = "beginner_cn"
DEFAULT_RECOMMENDATION_PROFILE_PATH = (
    REPO_ROOT / "config" / "recommendation_profiles" / "beginner_cn.toml"
)


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(section: Mapping[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _str(section: Mapping[str, Any], key: str, default: str = "") -> str:
    value = section.get(key, default)
    return str(value).strip() if value is not None else default


def _float(section: Mapping[str, Any], key: str, default: float, *, minimum: Optional[float] = None) -> float:
    value = section.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _int(section: Mapping[str, Any], key: str, default: int, *, minimum: Optional[int] = None) -> int:
    value = section.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _str_list(section: Mapping[str, Any], key: str, default: List[str]) -> List[str]:
    value = section.get(key, default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default)


@dataclass(frozen=True)
class RecommendationProfile:
    """Validated strategy profile loaded from TOML."""

    name: str
    path: Path
    raw: Dict[str, Any]
    content: str
    profile_hash: str

    @property
    def universe(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("universe"))

    @property
    def snapshot(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("snapshot"))

    @property
    def hard_filters(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("hard_filters"))

    @property
    def features(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("features"))

    @property
    def scoring(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("scoring"))

    @property
    def output(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("output"))

    @property
    def strategies(self) -> Dict[str, Any]:
        return _as_dict(self.raw.get("strategies"))

    def preferred_sources(self) -> List[str]:
        return _str_list(self.snapshot, "preferred_sources", ["efinance", "akshare_em", "tencent", "akshare_sina"])

    def min_snapshot_coverage_ratio(self) -> float:
        return _float(self.snapshot, "min_snapshot_coverage_ratio", 0.85, minimum=0.0)

    def history_prefilter_limit(self) -> int:
        return _int(self.features, "history_prefilter_limit", 500, minimum=1)

    def candidate_limit(self) -> int:
        return _int(self.output, "candidate_limit", 300, minimum=1)

    def deep_analysis_limit(self) -> int:
        return _int(self.output, "deep_analysis_limit", 30, minimum=0)

    def recommend_limit(self) -> int:
        return _int(self.output, "recommend_limit", 10, minimum=1)

    def min_final_score(self) -> float:
        return _float(self.output, "min_final_score", 70.0, minimum=0.0)

    def weights(self) -> Dict[str, float]:
        defaults = {
            "liquidity": 15.0,
            "trend": 25.0,
            "entry_quality": 25.0,
            "volume_price": 15.0,
            "sector_strength": 10.0,
            "stability": 10.0,
        }
        section = _as_dict(self.scoring.get("weights"))
        return {
            key: _float(section, key, default, minimum=0.0)
            for key, default in defaults.items()
        }

    def strategy_config(self, name: str) -> Dict[str, Any]:
        return _as_dict(self.strategies.get(name))

    def strategy_enabled(self, name: str) -> bool:
        section = self.strategy_config(name)
        return _bool(section, "enabled", True)


def resolve_profile_path(profile: Optional[str] = None, profile_path: Optional[str] = None) -> Path:
    """Resolve a profile name/path into an absolute TOML path."""
    raw_path = (profile_path or "").strip()
    if raw_path:
        path = Path(os.path.expanduser(raw_path))
        return path if path.is_absolute() else REPO_ROOT / path

    profile_name = (profile or DEFAULT_RECOMMENDATION_PROFILE).strip() or DEFAULT_RECOMMENDATION_PROFILE
    candidate = REPO_ROOT / "config" / "recommendation_profiles" / f"{profile_name}.toml"
    return candidate


def load_recommendation_profile(profile: Optional[str] = None, profile_path: Optional[str] = None) -> RecommendationProfile:
    """Load and validate a recommendation profile."""
    path = resolve_profile_path(profile=profile, profile_path=profile_path)
    if not path.is_file():
        raise FileNotFoundError(f"推荐策略 profile 不存在: {path}")

    content = path.read_text(encoding="utf-8")
    raw = tomllib.loads(content)
    profile_name = (profile or path.stem or DEFAULT_RECOMMENDATION_PROFILE).strip()
    profile_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    loaded = RecommendationProfile(
        name=profile_name,
        path=path,
        raw=raw,
        content=content,
        profile_hash=profile_hash,
    )
    validate_recommendation_profile(loaded)
    return loaded


def validate_recommendation_profile(profile: RecommendationProfile) -> None:
    """Raise ValueError when profile invariants are invalid."""
    supported_weights = {
        "liquidity",
        "trend",
        "entry_quality",
        "volume_price",
        "sector_strength",
        "stability",
    }
    supported_strategies = {
        "trend_pullback",
        "volume_breakout",
        "strong_consolidation",
        "low_reversal",
    }
    output = profile.output
    candidate_limit = _int(output, "candidate_limit", 300, minimum=1)
    deep_analysis_limit = _int(output, "deep_analysis_limit", 30, minimum=0)
    recommend_limit = _int(output, "recommend_limit", 10, minimum=1)
    history_prefilter_limit = profile.history_prefilter_limit()
    if not (recommend_limit <= deep_analysis_limit <= candidate_limit <= history_prefilter_limit):
        raise ValueError(
            "推荐 profile 输出限制必须满足 recommend_limit <= deep_analysis_limit <= "
            "candidate_limit <= history_prefilter_limit"
        )

    if profile.min_snapshot_coverage_ratio() > 1:
        raise ValueError("min_snapshot_coverage_ratio 不能大于 1")

    weights_section = _as_dict(profile.scoring.get("weights"))
    for key, value in weights_section.items():
        if key not in supported_weights:
            raise ValueError(f"推荐 profile 不支持的评分权重: scoring.weights.{key}")
        _require_non_negative(value, f"scoring.weights.{key}")

    for strategy_name, strategy_cfg in profile.strategies.items():
        if strategy_name not in supported_strategies:
            raise ValueError(f"推荐 profile 不支持的策略: strategies.{strategy_name}")
        if isinstance(strategy_cfg, Mapping):
            for field in ("max_recommendations", "min_score", "max_bias_ma5", "max_change_pct", "max_drawdown_20d", "max_return_60d"):
                if field in strategy_cfg:
                    _require_non_negative(strategy_cfg[field], f"strategies.{strategy_name}.{field}")

    price_cfg = _as_dict(profile.hard_filters.get("price"))
    liquidity_cfg = _as_dict(profile.hard_filters.get("liquidity"))
    intraday_cfg = _as_dict(profile.hard_filters.get("intraday"))
    for field in ("min_price", "max_price"):
        if field in price_cfg:
            _require_non_negative(price_cfg[field], f"hard_filters.price.{field}")
    min_price = _float(price_cfg, "min_price", 0)
    max_price = _float(price_cfg, "max_price", 10**9)
    if max_price < min_price:
        raise ValueError("hard_filters.price.max_price 不能小于 min_price")

    for field in ("min_amount", "min_avg_amount_5d", "min_avg_amount_20d", "min_circ_mv"):
        if field in liquidity_cfg:
            _require_non_negative(liquidity_cfg[field], f"hard_filters.liquidity.{field}")
    for field in ("max_abs_change_pct", "max_amplitude", "max_volume_ratio", "max_turnover_rate"):
        if field in intraday_cfg:
            _require_non_negative(intraday_cfg[field], f"hard_filters.intraday.{field}")


def _require_non_negative(value: Any, field_name: str) -> None:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} 不能为负数")

# -*- coding: utf-8 -*-
"""Repository helpers for recommendation run indexes."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, func, or_, select

from src.storage import DatabaseManager, RecommendationRunIndex


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None


def _json_text(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


class RecommendationRunRepository:
    """DB access layer for recommendation run summaries."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert_from_meta(self, meta: Dict[str, Any], *, meta_file: str) -> None:
        run_id = str(meta.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("recommendation run meta missing run_id")

        summary = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
        trade_date = _parse_date(meta.get("trade_date"))
        if trade_date is None:
            raise ValueError("recommendation run meta missing trade_date")
        generated_at = _parse_datetime(meta.get("generated_at"))

        values = {
            "run_id": run_id,
            "market": str(meta.get("market") or "cn"),
            "trade_date": trade_date,
            "generated_at": generated_at,
            "profile": meta.get("profile"),
            "profile_hash": meta.get("profile_hash"),
            "universe_count": int(summary.get("universe_count") or 0),
            "snapshot_count": int(summary.get("snapshot_count") or 0),
            "scored_count": int(summary.get("scored_count") or 0),
            "recommended_count": int(summary.get("recommended_count") or 0),
            "deep_analyzed_count": int(summary.get("deep_analyzed_count") or 0),
            "coverage_ratio": float(summary.get("coverage_ratio")) if summary.get("coverage_ratio") is not None else None,
            "meta_file": meta_file,
            "snapshot_file": meta.get("snapshot_file"),
            "candidates_file": meta.get("candidates_file"),
            "recommendations_file": meta.get("recommendations_file"),
            "profile_snapshot_file": meta.get("profile_snapshot_file"),
            "backtest_files": _json_text(meta.get("backtest_files") or {}),
            "warnings": _json_text(meta.get("warnings") or []),
            "updated_at": datetime.now(),
        }

        with self.db.get_session() as session:
            existing = session.execute(
                select(RecommendationRunIndex).where(RecommendationRunIndex.run_id == run_id).limit(1)
            ).scalar_one_or_none()
            if existing is None:
                session.add(RecommendationRunIndex(**values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            session.commit()

    def list_runs(
        self,
        *,
        market: str = "cn",
        page: int = 1,
        limit: int = 20,
        query: Optional[str] = None,
    ) -> Tuple[List[RecommendationRunIndex], int]:
        offset = max(page - 1, 0) * limit
        conditions = [RecommendationRunIndex.market == market]
        if query:
            like = f"%{query}%"
            conditions.append(
                or_(
                    RecommendationRunIndex.run_id.like(like),
                    RecommendationRunIndex.profile.like(like),
                    RecommendationRunIndex.profile_hash.like(like),
                )
            )
        where_clause = and_(*conditions)
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(RecommendationRunIndex.id)).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(RecommendationRunIndex)
                .where(where_clause)
                .order_by(desc(RecommendationRunIndex.trade_date), desc(RecommendationRunIndex.generated_at))
                .offset(offset)
                .limit(limit)
            ).scalars().all()
            return list(rows), int(total)

    def get_by_run_id(self, run_id: str) -> Optional[RecommendationRunIndex]:
        with self.db.get_session() as session:
            return session.execute(
                select(RecommendationRunIndex).where(RecommendationRunIndex.run_id == run_id).limit(1)
            ).scalar_one_or_none()

    @staticmethod
    def to_dict(row: RecommendationRunIndex) -> Dict[str, Any]:
        return {
            "run_id": row.run_id,
            "market": row.market,
            "trade_date": row.trade_date.isoformat() if row.trade_date else None,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            "profile": row.profile,
            "profile_hash": row.profile_hash,
            "summary": {
                "universe_count": row.universe_count,
                "snapshot_count": row.snapshot_count,
                "scored_count": row.scored_count,
                "recommended_count": row.recommended_count,
                "deep_analyzed_count": row.deep_analyzed_count,
                "coverage_ratio": row.coverage_ratio,
            },
            "meta_file": row.meta_file,
            "_meta_file": row.meta_file,
            "snapshot_file": row.snapshot_file,
            "candidates_file": row.candidates_file,
            "recommendations_file": row.recommendations_file,
            "profile_snapshot_file": row.profile_snapshot_file,
            "backtest_files": json.loads(row.backtest_files or "{}"),
            "warnings": json.loads(row.warnings or "[]"),
        }

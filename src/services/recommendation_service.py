# -*- coding: utf-8 -*-
"""Orchestration service for full-market recommendation runs."""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from src.config import Config, get_config
from src.services.recommendation_models import (
    CandidateScore,
    MarketSnapshotItem,
    RecommendationRunArtifacts,
)
from src.services.recommendation_profile import load_recommendation_profile
from src.services.recommendation_screener import HistoryLoader, RecommendationScreener
from src.services.recommendation_snapshot import MarketSnapshotFetcher
from src.services.stock_universe_service import load_stock_universe
from src.core.trading_calendar import (
    MarketPhase,
    build_market_phase_context,
    get_effective_trading_date,
    get_market_now,
)


TZ_CN = timezone(timedelta(hours=8))
AnalysisRunner = Callable[[str], Optional[Dict[str, Any]]]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_profile_hash_prefix(profile_hash: str) -> str:
    return profile_hash.split(":", 1)[-1][:6]


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class RecommendationService:
    """Run and read full-market recommendation outputs."""

    def __init__(
        self,
        config: Optional[Config] = None,
        snapshot_fetcher: Optional[MarketSnapshotFetcher] = None,
        history_loader: Optional[HistoryLoader] = None,
        analysis_runner: Optional[AnalysisRunner] = None,
        db_manager: Optional[Any] = None,
    ):
        self.config = config or get_config()
        self.snapshot_fetcher = snapshot_fetcher or MarketSnapshotFetcher()
        self.history_loader = history_loader
        self.analysis_runner = analysis_runner
        self.db_manager = db_manager

    @property
    def output_dir(self) -> Path:
        raw = getattr(self.config, "recommendation_output_dir", "data/recommendations")
        path = Path(str(raw))
        return path if path.is_absolute() else _repo_root() / path

    def run_once(
        self,
        *,
        force_refresh_snapshot: bool = False,
        run_deep_analysis: Optional[bool] = None,
        current_time: Optional[datetime] = None,
    ) -> RecommendationRunArtifacts:
        """Execute one full-market recommendation run and persist artifacts."""
        market = str(getattr(self.config, "recommendation_market", "cn") or "cn").lower()
        if market != "cn":
            raise ValueError("首版仅支持 A 股推荐市场: cn")

        now = self._resolve_run_time(market=market, current_time=current_time)
        trade_date = get_effective_trading_date(market, current_time=now)
        profile = load_recommendation_profile(
            profile=getattr(self.config, "recommendation_profile", "beginner_cn"),
            profile_path=getattr(self.config, "recommendation_profile_path", ""),
        )
        universe = load_stock_universe(
            market=market,
            asset_types=profile.universe.get("asset_types", ["stock"]),
        )
        snapshot_file = self.output_dir / "snapshots" / market / f"{trade_date.isoformat()}.market.csv"
        snapshot_refreshed = force_refresh_snapshot or not snapshot_file.is_file()
        if snapshot_refreshed:
            snapshot, data_sources = self.snapshot_fetcher.fetch(
                trade_date=trade_date,
                universe=universe,
                preferred_sources=profile.preferred_sources(),
            )
            self._write_snapshot(snapshot_file, snapshot)
        else:
            snapshot = self._read_snapshot(snapshot_file)
            data_sources = [{"name": "cached_snapshot", "status": "ok", "rows": len(snapshot)}]
        snapshot_by_code = {item.code: item for item in snapshot}
        coverage_ratio = (len(snapshot_by_code) / len(universe)) if universe else 0.0

        run_id = (
            f"{market}-{trade_date.isoformat()}-{now.strftime('%H%M%S')}-"
            f"{profile.name}-{_safe_profile_hash_prefix(profile.profile_hash)}"
        )
        run_stem = f"{now.strftime('%H%M%S')}-{profile.name}-{_safe_profile_hash_prefix(profile.profile_hash)}"
        run_dir = self.output_dir / "runs" / market / trade_date.isoformat()
        profile_dir = self.output_dir / "profiles" / market / trade_date.isoformat()
        candidates_file = run_dir / f"{run_stem}.candidates.csv"
        recommendations_file = run_dir / f"{run_stem}.recommendations.csv"
        meta_file = run_dir / f"{run_stem}.meta.json"
        profile_snapshot_file = profile_dir / f"{run_stem}.toml"

        warnings: List[str] = []
        summary: Dict[str, Any] = {
            "universe_count": len(universe),
            "snapshot_count": len(snapshot_by_code),
            "passed_hard_filters": 0,
            "scored_count": 0,
            "deep_analyzed_count": 0,
            "recommended_count": 0,
            "coverage_ratio": round(coverage_ratio, 4),
        }

        if coverage_ratio < profile.min_snapshot_coverage_ratio():
            warnings.append(
                f"snapshot coverage {coverage_ratio:.2%} below minimum {profile.min_snapshot_coverage_ratio():.2%}"
            )
            self._write_meta(
                meta_file=meta_file,
                run_id=run_id,
                market=market,
                trade_date=trade_date,
                generated_at=now,
                profile=profile,
                snapshot_file=snapshot_file,
                snapshot_refreshed=snapshot_refreshed,
                candidates_file=None,
                recommendations_file=None,
                profile_snapshot_file=profile_snapshot_file,
                summary=summary,
                data_sources=data_sources,
                warnings=warnings,
            )
            _atomic_write_text(profile_snapshot_file, profile.content)
            self._sync_index_from_meta_file(meta_file)
            self._cleanup_retention(now=now)
            return RecommendationRunArtifacts(
                run_id=run_id,
                market=market,
                trade_date=trade_date,
                generated_at=now,
                snapshot_file=snapshot_file,
                candidates_file=None,
                recommendations_file=None,
                meta_file=meta_file,
                profile_snapshot_file=profile_snapshot_file,
                summary=summary,
                warnings=warnings,
            )

        screener = RecommendationScreener(profile, history_loader=self.history_loader)
        result = screener.screen(
            trade_date=trade_date,
            universe=universe,
            snapshot=snapshot,
        )
        candidates = result.candidates
        recommendations = result.recommendations
        should_run_deep_analysis = (
            bool(run_deep_analysis)
            if run_deep_analysis is not None
            else bool(getattr(self.config, "recommendation_llm_review_enabled", False))
        )
        deep_analyzed_count = 0
        if should_run_deep_analysis:
            deep_analyzed_count = self._review_recommendations(
                recommendations,
                limit=profile.deep_analysis_limit(),
            )
        summary.update({
            "passed_hard_filters": len(snapshot_by_code) - result.filtered_count,
            "scored_count": len(candidates),
            "deep_analyzed_count": deep_analyzed_count,
            "recommended_count": len(recommendations),
            "history_loaded_count": result.history_loaded_count,
            "filter_breakdown": result.filter_breakdown,
        })
        warnings.extend(result.warnings)

        self._write_candidates(candidates_file, candidates)
        self._write_recommendations(recommendations_file, recommendations)
        _atomic_write_text(profile_snapshot_file, profile.content)
        self._write_meta(
            meta_file=meta_file,
            run_id=run_id,
            market=market,
            trade_date=trade_date,
            generated_at=now,
            profile=profile,
            snapshot_file=snapshot_file,
            snapshot_refreshed=snapshot_refreshed,
            candidates_file=candidates_file,
            recommendations_file=recommendations_file,
            profile_snapshot_file=profile_snapshot_file,
            summary=summary,
            data_sources=data_sources,
            warnings=warnings,
        )
        self._sync_index_from_meta_file(meta_file)
        self._cleanup_retention(now=now)
        return RecommendationRunArtifacts(
            run_id=run_id,
            market=market,
            trade_date=trade_date,
            generated_at=now,
            snapshot_file=snapshot_file,
            candidates_file=candidates_file,
            recommendations_file=recommendations_file,
            meta_file=meta_file,
            profile_snapshot_file=profile_snapshot_file,
            summary=summary,
            warnings=warnings,
            )

    def get_schedule_skip_reason(
        self,
        *,
        current_time: Optional[datetime] = None,
        force_run: bool = False,
    ) -> Optional[str]:
        """Return a human-readable reason when a scheduled recommendation should be skipped."""
        if force_run or not bool(getattr(self.config, "trading_day_check_enabled", True)):
            return None

        market = str(getattr(self.config, "recommendation_market", "cn") or "cn").lower()
        context = build_market_phase_context(
            market=market,
            current_time=current_time,
            trigger_source="schedule",
            analysis_intent="recommendation",
        )
        if context.phase == MarketPhase.UNKNOWN:
            return None
        if context.is_trading_day is False:
            return f"目标市场 {market} 今日非交易日"
        if context.phase in {
            MarketPhase.PREMARKET,
            MarketPhase.INTRADAY,
            MarketPhase.LUNCH_BREAK,
            MarketPhase.CLOSING_AUCTION,
        }:
            return f"目标市场 {market} 尚未收盘，当前阶段: {context.phase.value}"
        return None

    @staticmethod
    def _resolve_run_time(*, market: str, current_time: Optional[datetime]) -> datetime:
        if current_time is None:
            return get_market_now(market)
        return get_market_now(market, current_time=current_time)

    def list_runs(self, *, market: str = "cn", limit: int = 20) -> List[Dict[str, Any]]:
        return self.list_runs_paginated(market=market, page=1, limit=limit).get("items", [])

    def list_runs_paginated(
        self,
        *,
        market: str = "cn",
        page: int = 1,
        limit: int = 20,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        indexed = self._list_indexed_runs(market=market, page=page, limit=limit, query=query)
        if indexed is not None:
            return indexed

        base = self.output_dir / "runs" / market
        if not base.exists():
            return {"items": [], "total": 0, "page": page, "limit": limit, "source": "files"}
        metas = sorted(base.glob("*/*.meta.json"), reverse=True)
        if query:
            metas = [path for path in metas if query in path.name or query in str(path.parent.name)]
        total = len(metas)
        start = max(page - 1, 0) * limit
        selected = metas[start:start + max(limit, 1)]
        runs: List[Dict[str, Any]] = []
        for meta_path in selected:
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            data["_meta_file"] = str(meta_path)
            runs.append(data)
        return {"items": runs, "total": total, "page": page, "limit": limit, "source": "files"}

    def latest(self, *, market: str = "cn") -> Optional[Dict[str, Any]]:
        runs = self.list_runs(market=market, limit=1)
        if not runs:
            return None
        meta = self._load_full_meta(runs[0])
        recommendations_path = self._resolve_artifact_path(meta.get("recommendations_file"))
        recommendations = self._read_csv(recommendations_path) if recommendations_path else []
        return {
            "meta": meta,
            "recommendations": recommendations,
        }

    def get_run(self, run_id: str, *, market: str = "cn") -> Optional[Dict[str, Any]]:
        meta = self._find_run_meta(run_id, market=market)
        if meta is None:
            return None
        meta = self._load_full_meta(meta)
        recommendations_path = self._resolve_artifact_path(meta.get("recommendations_file"))
        candidates_path = self._resolve_artifact_path(meta.get("candidates_file"))
        return {
            "meta": meta,
            "recommendations": self._read_csv(recommendations_path) if recommendations_path else [],
            "candidates": self._read_csv(candidates_path) if candidates_path else [],
        }

    def _db_index_enabled(self) -> bool:
        return bool(getattr(self.config, "recommendation_db_index_enabled", False))

    def _repository(self):
        from src.repositories.recommendation_repo import RecommendationRunRepository

        return RecommendationRunRepository(self.db_manager)

    def _sync_index_from_meta_file(self, meta_file: Path) -> None:
        if not self._db_index_enabled():
            return
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            try:
                meta_path = str(meta_file.relative_to(self.output_dir))
            except ValueError:
                meta_path = str(meta_file)
            self._repository().upsert_from_meta(meta, meta_file=meta_path)
        except Exception as exc:  # noqa: BLE001 - DB index must not break artifact generation.
            import logging

            logging.getLogger(__name__).warning("[recommendation] DB index sync failed: %s", exc)

    def _list_indexed_runs(
        self,
        *,
        market: str,
        page: int,
        limit: int,
        query: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not self._db_index_enabled():
            return None
        try:
            repo = self._repository()
            rows, total = repo.list_runs(market=market, page=page, limit=limit, query=query)
            if total == 0:
                return None
            return {
                "items": [repo.to_dict(row) for row in rows],
                "total": total,
                "page": page,
                "limit": limit,
                "source": "db",
            }
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("[recommendation] DB index read failed: %s", exc)
            return None

    def _load_full_meta(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        meta_path = self._resolve_artifact_path(meta.get("_meta_file") or meta.get("meta_file"))
        if meta_path is None or not meta_path.is_file():
            return meta
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return meta
        try:
            payload["_meta_file"] = str(meta_path.relative_to(self.output_dir))
        except ValueError:
            payload["_meta_file"] = str(meta_path)
        return payload

    def _find_run_meta(self, run_id: str, *, market: str = "cn") -> Optional[Dict[str, Any]]:
        if self._db_index_enabled():
            try:
                row = self._repository().get_by_run_id(run_id)
                if row is not None and row.market == market:
                    return self._repository().to_dict(row)
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("[recommendation] DB index lookup failed: %s", exc)

        base = self.output_dir / "runs" / market
        if not base.exists():
            return None
        for meta_path in sorted(base.glob("*/*.meta.json"), reverse=True):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("run_id") != run_id:
                continue
            try:
                meta["_meta_file"] = str(meta_path.relative_to(self.output_dir))
            except ValueError:
                meta["_meta_file"] = str(meta_path)
            return meta
        return None

    def get_run_file(self, run_id: str, kind: str, *, market: str = "cn") -> Optional[Path]:
        """Return a persisted artifact path for a run without recalculating recommendations."""
        artifact_keys = {
            "market": "snapshot_file",
            "snapshot": "snapshot_file",
            "candidates": "candidates_file",
            "recommendations": "recommendations_file",
            "backtest": "backtest_file",
            "meta": "_meta_file",
            "profile": "profile_snapshot_file",
        }
        artifact_key = artifact_keys.get((kind or "").strip().lower())
        if artifact_key is None:
            raise ValueError("不支持的推荐文件类型")

        meta = self._find_run_meta(run_id, market=market)
        if meta is None:
            return None
        meta = self._load_full_meta(meta)
        if artifact_key == "_meta_file":
            path = self._resolve_artifact_path(meta.get("_meta_file") or meta.get("meta_file"))
        elif artifact_key == "backtest_file":
            path = self._resolve_backtest_file(meta)
        else:
            path = self._resolve_artifact_path(meta.get(artifact_key))
        if path is None or not path.is_file():
            return None
        return path

    def run_backtest(
        self,
        run_id: str,
        *,
        market: str = "cn",
        windows: Sequence[int] = (3, 5, 10, 20),
    ) -> Dict[str, Any]:
        """Backtest one persisted recommendation run against later daily bars."""
        result = self.get_run(run_id, market=market)
        if result is None:
            raise FileNotFoundError(f"未找到推荐运行: {run_id}")

        meta = result.get("meta", {})
        recommendations = result.get("recommendations", [])
        trade_date = date.fromisoformat(str(meta.get("trade_date") or "")[:10])
        snapshot_path = self._resolve_artifact_path(meta.get("snapshot_file"))
        snapshot_by_code = {item.code: item for item in self._read_snapshot(snapshot_path)} if snapshot_path else {}
        normalized_windows = sorted({int(window) for window in windows if int(window) > 0})
        if not normalized_windows:
            raise ValueError("回测窗口不能为空")

        rows: List[Dict[str, Any]] = []
        completed = 0
        insufficient = 0
        errors = 0
        for recommendation in recommendations:
            code = str(recommendation.get("code") or "").strip()
            snapshot = snapshot_by_code.get(code)
            start_price = self._float(snapshot.price if snapshot else None)
            if start_price is None or start_price <= 0:
                start_price = self._float(recommendation.get("watch_price"))

            base_row = {
                "run_id": run_id,
                "profile_hash": meta.get("profile_hash", ""),
                "trade_date": trade_date.isoformat(),
                "code": code,
                "name": recommendation.get("name", ""),
                "rank": recommendation.get("rank", ""),
                "strategy": recommendation.get("strategy", ""),
                "selection_score": recommendation.get("selection_score", ""),
                "recommendation_level": recommendation.get("recommendation_level", ""),
                "watch_price": recommendation.get("watch_price", ""),
                "stop_loss": recommendation.get("stop_loss", ""),
                "take_profit_reference": recommendation.get("take_profit_reference", ""),
                "start_price": start_price,
                "eval_status": "completed",
                "error": "",
            }
            try:
                bars = self._load_future_daily_bars(code, trade_date, max(normalized_windows))
                if start_price is None or not bars:
                    insufficient += 1
                    base_row["eval_status"] = "insufficient_data"
                else:
                    self._append_backtest_metrics(
                        base_row,
                        bars=bars,
                        start_price=start_price,
                        stop_loss=self._float(recommendation.get("stop_loss")),
                        take_profit=self._float(recommendation.get("take_profit_reference")),
                        windows=normalized_windows,
                    )
                    if all(base_row.get(f"status_{window}d") == "completed" for window in normalized_windows):
                        completed += 1
                    else:
                        insufficient += 1
                rows.append(base_row)
            except Exception as exc:  # noqa: BLE001 - keep per-stock backtests isolated.
                errors += 1
                base_row["eval_status"] = "error"
                base_row["error"] = str(exc)
                rows.append(base_row)

        backtest_file = self._backtest_file_for_meta(meta, normalized_windows)
        fieldnames = self._backtest_fieldnames(normalized_windows)
        _write_csv(backtest_file, rows, fieldnames)
        self._record_backtest_file(meta, backtest_file, normalized_windows)
        summary = {
            "run_id": run_id,
            "market": market,
            "windows": normalized_windows,
            "processed": len(recommendations),
            "completed": completed,
            "insufficient": insufficient,
            "errors": errors,
            "backtest_file": str(backtest_file),
        }
        return summary

    def build_notification_summary(self, run_id: str, *, market: str = "cn", limit: int = 5) -> Optional[str]:
        """Build a concise Markdown notification from persisted recommendation artifacts."""
        result = self.get_run(run_id, market=market)
        if result is None:
            return None

        meta = result.get("meta", {})
        recommendations = result.get("recommendations", [])
        summary = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
        warnings = meta.get("warnings") if isinstance(meta.get("warnings"), list) else []

        trade_date = meta.get("trade_date", "")
        generated_at = str(meta.get("generated_at", ""))[:19].replace("T", " ")
        recommended_count = summary.get("recommended_count", len(recommendations))
        scored_count = summary.get("scored_count", "-")
        snapshot_count = summary.get("snapshot_count", "-")

        lines = [
            "# 盘后选股推荐",
            "",
            f"- 交易日：{trade_date or '-'}",
            f"- 生成时间：{generated_at or '-'}",
            f"- 推荐数量：{recommended_count}，评分候选：{scored_count}，快照覆盖：{snapshot_count}",
            f"- 运行 ID：`{run_id}`",
            "",
        ]
        if warnings:
            lines.extend([
                "## 运行提示",
                *[f"- {warning}" for warning in warnings[:3]],
                "",
            ])

        if not recommendations:
            lines.extend([
                "## 今日结论",
                "未生成最终推荐。请查看运行提示、快照覆盖率和过滤规则。",
            ])
            return "\n".join(lines).strip()

        lines.append("## 推荐关注")
        for row in recommendations[: max(1, limit)]:
            rank = row.get("rank") or "-"
            name = row.get("name") or "-"
            code = row.get("code") or "-"
            label = row.get("recommendation_label") or row.get("recommendationLevel") or "观察"
            score = row.get("selection_score") or "-"
            action = row.get("beginner_action") or row.get("no_position_action") or "观察，不追高"
            watch_price = row.get("watch_price") or "-"
            stop_loss = row.get("stop_loss") or "-"
            reasons = str(row.get("positive_reasons") or "").replace(";", "；")
            lines.append(f"{rank}. **{name} ({code})**：{label}，分数 {score}")
            lines.append(f"   - 动作：{action}")
            lines.append(f"   - 关注价：{watch_price}；止损：{stop_loss}")
            if reasons:
                lines.append(f"   - 理由：{reasons}")
        lines.extend([
            "",
            "仅作为盘后观察清单，不构成买卖建议；执行前需结合次日开盘、仓位和风险承受能力确认。",
        ])
        return "\n".join(lines).strip()

    def _review_recommendations(self, recommendations: List[CandidateScore], *, limit: int) -> int:
        reviewed = 0
        runner = self.analysis_runner or self._default_analysis_runner
        for candidate in recommendations[: max(0, limit)]:
            try:
                analysis = runner(candidate.code)
            except Exception as exc:  # noqa: BLE001 - one review must not fail the run.
                candidate.llm_review_status = "downgraded"
                candidate.negative_reasons.append(f"LLM复核失败: {exc}")
                reviewed += 1
                continue

            if not analysis:
                candidate.llm_review_status = "downgraded"
                candidate.negative_reasons.append("LLM复核未返回有效结果")
                reviewed += 1
                continue

            candidate.analysis_query_id = str(analysis.get("query_id") or "")
            status, reason = self._classify_llm_review(analysis)
            candidate.llm_review_status = status
            if reason:
                candidate.negative_reasons.append(f"LLM复核: {reason}")
            if status == "downgraded":
                candidate.recommendation_level = "watch_only"
                candidate.recommendation_label = "只看不追"
                candidate.beginner_action = "LLM 复核提示风险，先观察，不追高"
            elif status == "rejected":
                candidate.recommendation_level = "watch_only"
                candidate.recommendation_label = "复核不通过"
                candidate.beginner_action = "LLM 复核不通过，仅保留审计记录"
            reviewed += 1
        return reviewed

    @staticmethod
    def _default_analysis_runner(code: str) -> Optional[Dict[str, Any]]:
        from src.services.analysis_service import AnalysisService

        return AnalysisService().analyze_stock(
            code,
            report_type="detailed",
            force_refresh=False,
            send_notification=False,
        )

    @staticmethod
    def _classify_llm_review(analysis: Dict[str, Any]) -> tuple[str, str]:
        report = analysis.get("report") if isinstance(analysis.get("report"), dict) else {}
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        details = report.get("details") if isinstance(report.get("details"), dict) else {}
        advice = str(summary.get("operation_advice") or "")
        trend = str(summary.get("trend_prediction") or "")
        risk_warning = str(details.get("risk_warning") or "")
        sentiment_score = RecommendationService._float(summary.get("sentiment_score"))
        combined = f"{advice} {trend} {risk_warning}"

        reject_keywords = ("卖出", "清仓", "减仓", "强烈风险", "不建议关注", "回避", "avoid", "sell")
        downgrade_keywords = ("观望", "等待", "谨慎", "风险", "震荡", "wait", "watch")
        if any(keyword.lower() in combined.lower() for keyword in reject_keywords):
            return "rejected", "深度分析建议回避或降低仓位"
        if sentiment_score is not None and sentiment_score < 35:
            return "rejected", f"情绪分过低({sentiment_score:g})"
        if sentiment_score is not None and sentiment_score < 50:
            return "downgraded", f"情绪分偏低({sentiment_score:g})"
        if any(keyword.lower() in combined.lower() for keyword in downgrade_keywords):
            return "downgraded", "深度分析提示需要等待或谨慎观察"
        return "passed", ""

    def _resolve_artifact_path(self, value: Any) -> Optional[Path]:
        if not value:
            return None
        path = Path(str(value))
        if path.is_absolute():
            return path
        return self.output_dir / path

    @staticmethod
    def _read_csv(path: Optional[Path]) -> List[Dict[str, Any]]:
        if path is None or not path.is_file():
            return []
        with path.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed

    @staticmethod
    def _find_daily_column(rows: List[Dict[str, Any]], names: Sequence[str]) -> Optional[str]:
        if not rows:
            return None
        keys = rows[0].keys()
        for name in names:
            if name in keys:
                return name
        lower_map = {str(key).lower(): key for key in keys}
        for name in names:
            matched = lower_map.get(name.lower())
            if matched is not None:
                return str(matched)
        return None

    def _load_future_daily_bars(self, code: str, trade_date: date, max_window: int) -> List[Dict[str, Any]]:
        loader = self.history_loader or RecommendationScreener._default_history_loader
        df, _source = loader(code, max(120, max_window + 40))
        if df is None or getattr(df, "empty", True):
            return []
        rows = df.to_dict("records")
        date_col = self._find_daily_column(rows, ["date", "trade_date", "日期", "交易日期"])
        close_col = self._find_daily_column(rows, ["close", "收盘", "收盘价"])
        high_col = self._find_daily_column(rows, ["high", "最高", "最高价"]) or close_col
        low_col = self._find_daily_column(rows, ["low", "最低", "最低价"]) or close_col
        if date_col is None or close_col is None:
            return []

        bars: List[Dict[str, Any]] = []
        for row in rows:
            try:
                bar_date = date.fromisoformat(str(row.get(date_col) or "")[:10])
            except ValueError:
                continue
            if bar_date <= trade_date:
                continue
            close = self._float(row.get(close_col))
            high = self._float(row.get(high_col))
            low = self._float(row.get(low_col))
            if close is None:
                continue
            bars.append({
                "date": bar_date,
                "close": close,
                "high": high if high is not None else close,
                "low": low if low is not None else close,
            })
        bars.sort(key=lambda item: item["date"])
        return bars

    @staticmethod
    def _append_backtest_metrics(
        row: Dict[str, Any],
        *,
        bars: List[Dict[str, Any]],
        start_price: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        windows: Sequence[int],
    ) -> None:
        for window in windows:
            window_bars = bars[:window]
            if len(window_bars) < window:
                row[f"status_{window}d"] = "insufficient_data"
                continue
            end_close = RecommendationService._float(window_bars[-1].get("close"))
            max_high = max(float(bar["high"]) for bar in window_bars)
            min_low = min(float(bar["low"]) for bar in window_bars)
            row[f"status_{window}d"] = "completed"
            row[f"end_return_{window}d_pct"] = round((end_close - start_price) / start_price * 100, 4) if end_close is not None else ""
            row[f"max_gain_{window}d_pct"] = round((max_high - start_price) / start_price * 100, 4)
            row[f"max_drawdown_{window}d_pct"] = round((min_low - start_price) / start_price * 100, 4)
            row[f"hit_stop_loss_{window}d"] = bool(stop_loss is not None and min_low <= stop_loss)
            row[f"hit_take_profit_{window}d"] = bool(take_profit is not None and max_high >= take_profit)

    def _backtest_file_for_meta(self, meta: Dict[str, Any], windows: Sequence[int]) -> Path:
        meta_file = self._resolve_artifact_path(meta.get("_meta_file") or meta.get("meta_file"))
        if meta_file is None:
            raise FileNotFoundError("推荐运行 meta 文件不存在，无法写入回测结果")
        if not meta_file.is_file():
            raise FileNotFoundError("推荐运行 meta 文件不存在，无法写入回测结果")
        suffix = "-".join(str(window) for window in windows)
        return meta_file.with_name(meta_file.name.replace(".meta.json", f".backtest-{suffix}d.csv"))

    def _resolve_backtest_file(self, meta: Dict[str, Any]) -> Optional[Path]:
        files = meta.get("backtest_files")
        if isinstance(files, dict) and files:
            latest_key = sorted(files)[-1]
            return self._resolve_artifact_path(files.get(latest_key))
        meta_file = self._resolve_artifact_path(meta.get("_meta_file") or meta.get("meta_file"))
        if meta_file is None:
            return None
        if meta_file.is_file():
            matches = sorted(meta_file.parent.glob(meta_file.name.replace(".meta.json", ".backtest-*.csv")))
            if matches:
                return matches[-1]
        return None

    def _record_backtest_file(self, meta: Dict[str, Any], backtest_file: Path, windows: Sequence[int]) -> None:
        meta_file = self._resolve_artifact_path(meta.get("_meta_file") or meta.get("meta_file"))
        if meta_file is None:
            return
        if not meta_file.is_file():
            return
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        files = payload.get("backtest_files")
        if not isinstance(files, dict):
            files = {}
        key = "-".join(str(window) for window in windows) + "d"
        try:
            files[key] = str(backtest_file.relative_to(self.output_dir))
        except ValueError:
            files[key] = str(backtest_file)
        payload["backtest_files"] = files
        _atomic_write_text(meta_file, json.dumps(payload, ensure_ascii=False, indent=2))
        self._sync_index_from_meta_file(meta_file)

    @staticmethod
    def _backtest_fieldnames(windows: Sequence[int]) -> List[str]:
        fields = [
            "run_id",
            "profile_hash",
            "trade_date",
            "code",
            "name",
            "rank",
            "strategy",
            "selection_score",
            "recommendation_level",
            "watch_price",
            "stop_loss",
            "take_profit_reference",
            "start_price",
            "eval_status",
            "error",
        ]
        for window in windows:
            fields.extend([
                f"status_{window}d",
                f"end_return_{window}d_pct",
                f"max_gain_{window}d_pct",
                f"max_drawdown_{window}d_pct",
                f"hit_stop_loss_{window}d",
                f"hit_take_profit_{window}d",
            ])
        return fields

    @staticmethod
    def _read_snapshot(path: Path) -> List[MarketSnapshotItem]:
        rows = RecommendationService._read_csv(path)

        def as_float(value: Any) -> Optional[float]:
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def as_int(value: Any) -> Optional[int]:
            parsed = as_float(value)
            return int(parsed) if parsed is not None else None

        snapshot: List[MarketSnapshotItem] = []
        for row in rows:
            try:
                row_date = date.fromisoformat(str(row.get("trade_date") or "")[:10])
            except ValueError:
                row_date = date.today()
            snapshot.append(
                MarketSnapshotItem(
                    trade_date=row_date,
                    code=str(row.get("code") or "").strip(),
                    name=str(row.get("name") or "").strip(),
                    price=as_float(row.get("price")),
                    change_pct=as_float(row.get("change_pct")),
                    change_amount=as_float(row.get("change_amount")),
                    volume=as_int(row.get("volume")),
                    amount=as_float(row.get("amount")),
                    volume_ratio=as_float(row.get("volume_ratio")),
                    turnover_rate=as_float(row.get("turnover_rate")),
                    amplitude=as_float(row.get("amplitude")),
                    open=as_float(row.get("open")),
                    high=as_float(row.get("high")),
                    low=as_float(row.get("low")),
                    pre_close=as_float(row.get("pre_close")),
                    pe_ratio=as_float(row.get("pe_ratio")),
                    pb_ratio=as_float(row.get("pb_ratio")),
                    total_mv=as_float(row.get("total_mv")),
                    circ_mv=as_float(row.get("circ_mv")),
                    change_60d=as_float(row.get("change_60d")),
                    high_52w=as_float(row.get("high_52w")),
                    low_52w=as_float(row.get("low_52w")),
                    source=str(row.get("source") or ""),
                    data_quality=str(row.get("data_quality") or "ok"),
                )
            )
        return snapshot

    @staticmethod
    def _write_snapshot(path: Path, snapshot: List[MarketSnapshotItem]) -> None:
        fieldnames = list(MarketSnapshotItem(trade_date=date.today(), code="", name="").to_csv_row().keys())
        _write_csv(path, (item.to_csv_row() for item in snapshot), fieldnames)

    @staticmethod
    def _write_candidates(path: Path, candidates: List[CandidateScore]) -> None:
        fieldnames = list(CandidateScore(trade_date=date.today(), code="", name="").to_csv_row().keys())
        _write_csv(path, (item.to_csv_row() for item in candidates), fieldnames)

    @staticmethod
    def _write_recommendations(path: Path, recommendations: List[CandidateScore]) -> None:
        fieldnames = list(CandidateScore(trade_date=date.today(), code="", name="").to_csv_row(include_recommendation_fields=True).keys())
        _write_csv(
            path,
            (item.to_csv_row(include_recommendation_fields=True) for item in recommendations),
            fieldnames,
        )

    def _write_meta(
        self,
        *,
        meta_file: Path,
        run_id: str,
        market: str,
        trade_date: date,
        generated_at: datetime,
        profile: Any,
        snapshot_file: Path,
        snapshot_refreshed: bool,
        candidates_file: Optional[Path],
        recommendations_file: Optional[Path],
        profile_snapshot_file: Path,
        summary: Dict[str, Any],
        data_sources: List[Dict[str, Any]],
        warnings: List[str],
    ) -> None:
        def rel(path: Optional[Path]) -> Optional[str]:
            if path is None:
                return None
            try:
                return str(path.relative_to(self.output_dir))
            except ValueError:
                return str(path)

        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "market": market,
            "trade_date": trade_date.isoformat(),
            "generated_at": generated_at.isoformat(),
            "timezone": "Asia/Shanghai",
            "profile": profile.name,
            "profile_hash": profile.profile_hash,
            "snapshot_file": rel(snapshot_file),
            "snapshot_refreshed": snapshot_refreshed,
            "candidates_file": rel(candidates_file),
            "recommendations_file": rel(recommendations_file),
            "profile_snapshot_file": rel(profile_snapshot_file),
            "summary": summary,
            "data_sources": data_sources,
            "warnings": warnings,
        }
        _atomic_write_text(meta_file, json.dumps(payload, ensure_ascii=False, indent=2))

    def _cleanup_retention(self, *, now: Optional[datetime] = None) -> None:
        retention_days = int(getattr(self.config, "recommendation_snapshot_retention_days", 90) or 90)
        if retention_days <= 0:
            return
        cutoff = (now or datetime.now(TZ_CN)) - timedelta(days=retention_days)
        managed_roots = [
            self.output_dir / "snapshots",
            self.output_dir / "runs",
            self.output_dir / "profiles",
        ]
        for root in managed_roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    modified_at = datetime.fromtimestamp(path.stat().st_mtime, TZ_CN)
                except OSError:
                    continue
                if modified_at < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        continue
            self._prune_empty_dirs(root)

    @staticmethod
    def _prune_empty_dirs(root: Path) -> None:
        if not root.exists():
            return
        for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass

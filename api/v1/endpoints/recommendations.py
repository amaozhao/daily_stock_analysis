# -*- coding: utf-8 -*-
"""Full-market stock recommendation endpoints."""

from __future__ import annotations

from typing import Optional

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from api.deps import get_config_dep
from src.config import Config
from src.services.recommendation_service import RecommendationService

router = APIRouter()


@router.post("/run")
def run_recommendations(
    force_refresh_snapshot: bool = Query(False, description="是否强制刷新当日全市场快照"),
    run_deep_analysis: Optional[bool] = Query(None, description="是否对 Top 推荐运行 LLM 深度复核"),
    config: Config = Depends(get_config_dep),
):
    """Run one full-market recommendation screening job."""
    try:
        artifacts = RecommendationService(config=config).run_once(
            force_refresh_snapshot=force_refresh_snapshot,
            run_deep_analysis=run_deep_analysis,
        )
        return artifacts.to_response()
    except Exception as exc:  # noqa: BLE001 - surface actionable API error.
        raise HTTPException(
            status_code=500,
            detail={
                "error": "recommendation_run_failed",
                "message": str(exc),
            },
        ) from exc


@router.get("/latest")
def get_latest_recommendations(
    market: str = Query("cn", description="市场，首版支持 cn"),
    config: Config = Depends(get_config_dep),
):
    """Return latest persisted recommendation run and recommendation rows."""
    result = RecommendationService(config=config).latest(market=market)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "recommendation_not_found",
                "message": "暂无盘后推荐结果",
            },
        )
    return result


@router.get("/runs")
def list_recommendation_runs(
    market: str = Query("cn", description="市场，首版支持 cn"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    query: Optional[str] = Query(None, description="按 run_id/profile/profile_hash 搜索"),
    config: Config = Depends(get_config_dep),
):
    """List persisted recommendation runs."""
    return RecommendationService(config=config).list_runs_paginated(
        market=market,
        page=page,
        limit=limit,
        query=query,
    )


@router.post("/runs/{run_id}/backtest")
def run_recommendation_backtest(
    run_id: str,
    market: str = Query("cn", description="市场，首版支持 cn"),
    windows: str = Query("3,5,10,20", description="逗号分隔的回测交易日窗口"),
    config: Config = Depends(get_config_dep),
):
    """Run file-based backtest for one persisted recommendation run."""
    try:
        parsed_windows = _parse_backtest_windows(windows)
        return RecommendationService(config=config).run_backtest(
            run_id,
            market=market,
            windows=parsed_windows,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_backtest_windows",
                "message": str(exc),
            },
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "recommendation_run_not_found",
                "message": str(exc),
            },
        ) from exc


@router.get("/runs/{run_id}/files/{kind}")
def download_recommendation_run_file(
    run_id: str,
    kind: str,
    market: str = Query("cn", description="市场，首版支持 cn"),
    config: Config = Depends(get_config_dep),
):
    """Download one persisted recommendation artifact for audit and backtesting."""
    try:
        path = RecommendationService(config=config).get_run_file(run_id, kind, market=market)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_recommendation_file_kind",
                "message": str(exc),
            },
        ) from exc
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "recommendation_file_not_found",
                "message": f"未找到推荐运行文件: {run_id}/{kind}",
            },
        )

    filename = _download_filename(path, kind)
    media_type = "application/json" if path.suffix == ".json" else "text/csv"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/runs/{run_id}")
def get_recommendation_run(
    run_id: str,
    market: str = Query("cn", description="市场，首版支持 cn"),
    config: Config = Depends(get_config_dep),
):
    """Return one persisted recommendation run with candidates and final recommendations."""
    result = RecommendationService(config=config).get_run(run_id, market=market)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "recommendation_run_not_found",
                "message": f"未找到推荐运行: {run_id}",
            },
        )
    return result


def _download_filename(path: Path, kind: str) -> str:
    safe_kind = (kind or path.stem).strip().lower().replace("/", "_")
    suffix = path.suffix or ".csv"
    return f"{safe_kind}{suffix}" if not path.name.startswith(safe_kind) else path.name


def _parse_backtest_windows(value: str) -> list[int]:
    windows: list[int] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            parsed = int(item)
        except ValueError as exc:
            raise ValueError("回测窗口必须是正整数") from exc
        if parsed <= 0 or parsed > 120:
            raise ValueError("回测窗口必须在 1 到 120 个交易日之间")
        windows.append(parsed)
    if not windows:
        raise ValueError("回测窗口不能为空")
    return sorted(set(windows))

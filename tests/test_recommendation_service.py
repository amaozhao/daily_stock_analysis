# -*- coding: utf-8 -*-
"""Tests for full-market recommendation screening MVP."""

from __future__ import annotations

import os
import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi.responses import FileResponse

from api.v1.endpoints import recommendations as recommendations_endpoint
from src.services.recommendation_models import MarketSnapshotItem, StockUniverseItem
from src.services.recommendation_profile import load_recommendation_profile
from src.services.recommendation_screener import RecommendationScreener
from src.services.recommendation_service import RecommendationService
from src.core.trading_calendar import MarketPhase
from src.storage import DatabaseManager, RecommendationRunIndex


def _history_df(start: float = 10.0) -> pd.DataFrame:
    closes = []
    value = start
    for idx in range(90):
        value *= 1.003
        if idx > 82:
            value *= 0.998
        closes.append(round(value, 3))
    return pd.DataFrame({
        "close": closes,
        "high": [round(v * 1.015, 3) for v in closes],
        "low": [round(v * 0.985, 3) for v in closes],
        "volume": [12000000 + i * 1000 for i in range(len(closes))],
        "amount": [180000000 + i * 10000 for i in range(len(closes))],
    })


def _future_history_df(trade_date: date, start: float = 10.0) -> pd.DataFrame:
    rows = []
    close = start
    for idx in range(1, 26):
        close *= 1.01
        day = trade_date + timedelta(days=idx)
        rows.append({
            "date": day.isoformat(),
            "close": round(close, 3),
            "high": round(close * 1.02, 3),
            "low": round(close * 0.98, 3),
        })
    return pd.DataFrame(rows)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class _FakeSnapshotFetcher:
    def fetch(self, *, trade_date, universe, preferred_sources):
        items = [
            MarketSnapshotItem(
                trade_date=trade_date,
                code=item.display_code,
                name=item.name,
                price=13.0 + idx,
                change_pct=0.8 + idx * 0.2,
                volume=15000000,
                amount=300000000 + idx * 10000000,
                volume_ratio=0.9,
                turnover_rate=3.0,
                amplitude=3.0,
                open=12.9 + idx,
                high=13.2 + idx,
                low=12.7 + idx,
                circ_mv=8000000000,
                total_mv=12000000000,
                source="fake",
                data_quality="ok",
            )
            for idx, item in enumerate(universe)
        ]
        return items, [{"name": "fake", "status": "ok", "rows": len(items)}]


class RecommendationServiceTestCase(unittest.TestCase):
    def test_default_profile_loads_and_validates(self) -> None:
        profile = load_recommendation_profile("beginner_cn")
        self.assertEqual(profile.name, "beginner_cn")
        self.assertEqual(profile.recommend_limit(), 10)
        self.assertIn("sha256:", profile.profile_hash)

    def test_profile_validation_rejects_invalid_thresholds_and_unknown_strategy(self) -> None:
        base_profile = Path("config/recommendation_profiles/beginner_cn.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_threshold = Path(temp_dir) / "bad_threshold.toml"
            bad_threshold.write_text(
                base_profile.replace("min_amount = 100000000", "min_amount = -1"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "min_amount"):
                load_recommendation_profile(profile_path=str(bad_threshold))

            unknown_strategy = Path(temp_dir) / "unknown_strategy.toml"
            unknown_strategy.write_text(
                base_profile + "\n[strategies.magic_alpha]\nenabled = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不支持的策略"):
                load_recommendation_profile(profile_path=str(unknown_strategy))

    def test_schedule_gate_skips_non_trading_and_intraday_but_allows_postmarket(self) -> None:
        service = RecommendationService(config=SimpleNamespace(
            recommendation_market="cn",
            trading_day_check_enabled=True,
        ))

        with patch(
            "src.services.recommendation_service.build_market_phase_context",
            return_value=SimpleNamespace(
                phase=MarketPhase.NON_TRADING,
                is_trading_day=False,
            ),
        ):
            self.assertIn("非交易日", service.get_schedule_skip_reason())

        with patch(
            "src.services.recommendation_service.build_market_phase_context",
            return_value=SimpleNamespace(
                phase=MarketPhase.INTRADAY,
                is_trading_day=True,
            ),
        ):
            self.assertIn("尚未收盘", service.get_schedule_skip_reason())

        with patch(
            "src.services.recommendation_service.build_market_phase_context",
            return_value=SimpleNamespace(
                phase=MarketPhase.POSTMARKET,
                is_trading_day=True,
            ),
        ):
            self.assertIsNone(service.get_schedule_skip_reason())

    def test_schedule_gate_respects_force_run_and_disabled_trading_check(self) -> None:
        service = RecommendationService(config=SimpleNamespace(
            recommendation_market="cn",
            trading_day_check_enabled=True,
        ))
        with patch("src.services.recommendation_service.build_market_phase_context") as phase_context:
            self.assertIsNone(service.get_schedule_skip_reason(force_run=True))
            phase_context.assert_not_called()

        disabled_service = RecommendationService(config=SimpleNamespace(
            recommendation_market="cn",
            trading_day_check_enabled=False,
        ))
        with patch("src.services.recommendation_service.build_market_phase_context") as phase_context:
            self.assertIsNone(disabled_service.get_schedule_skip_reason())
            phase_context.assert_not_called()

    def test_run_once_persists_snapshot_candidates_recommendations_and_meta(self) -> None:
        universe = [
            StockUniverseItem(
                code=f"{600000 + idx}.SH",
                display_code=f"{600000 + idx}",
                name=f"测试股票{idx}",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            )
            for idx in range(12)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                recommendation_output_dir=temp_dir,
                recommendation_market="cn",
                recommendation_profile="beginner_cn",
                recommendation_profile_path="config/recommendation_profiles/beginner_cn.toml",
            )

            def history_loader(code: str, days: int):
                return _history_df(10.0 + int(code[-1])), "fake"

            with patch("src.services.recommendation_service.load_stock_universe", return_value=universe):
                artifacts = RecommendationService(
                    config=config,
                    snapshot_fetcher=_FakeSnapshotFetcher(),
                    history_loader=history_loader,
                ).run_once()

            self.assertTrue(artifacts.snapshot_file.is_file())
            self.assertTrue(artifacts.candidates_file and artifacts.candidates_file.is_file())
            self.assertTrue(artifacts.recommendations_file and artifacts.recommendations_file.is_file())
            self.assertTrue(artifacts.meta_file.is_file())
            self.assertTrue(artifacts.profile_snapshot_file.is_file())
            self.assertEqual(artifacts.summary["universe_count"], 12)
            self.assertEqual(artifacts.summary["snapshot_count"], 12)
            self.assertGreaterEqual(artifacts.summary["scored_count"], 1)

            latest = RecommendationService(config=config).latest()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["meta"]["run_id"], artifacts.run_id)
            self.assertIn("recommendations", latest)

            recommendations_file = RecommendationService(config=config).get_run_file(
                artifacts.run_id,
                "recommendations",
            )
            self.assertEqual(recommendations_file, artifacts.recommendations_file)

            file_response = recommendations_endpoint.download_recommendation_run_file(
                run_id=artifacts.run_id,
                kind="recommendations",
                market="cn",
                config=config,
            )
            self.assertIsInstance(file_response, FileResponse)
            self.assertEqual(Path(file_response.path), artifacts.recommendations_file)

            notification_summary = RecommendationService(config=config).build_notification_summary(
                artifacts.run_id,
            )
            self.assertIsNotNone(notification_summary)
            self.assertIn("盘后选股推荐", notification_summary)
            self.assertIn(artifacts.run_id, notification_summary)

    def test_run_once_uses_effective_trading_date_for_artifact_paths(self) -> None:
        universe = [
            StockUniverseItem(
                code=f"{600300 + idx}.SH",
                display_code=f"{600300 + idx}",
                name=f"有效日期股票{idx}",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            )
            for idx in range(12)
        ]
        effective_date = date(2026, 5, 29)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                recommendation_output_dir=temp_dir,
                recommendation_market="cn",
                recommendation_profile="beginner_cn",
                recommendation_profile_path="config/recommendation_profiles/beginner_cn.toml",
            )
            with patch("src.services.recommendation_service.load_stock_universe", return_value=universe), \
                 patch("src.services.recommendation_service.get_effective_trading_date", return_value=effective_date):
                artifacts = RecommendationService(
                    config=config,
                    snapshot_fetcher=_FakeSnapshotFetcher(),
                    history_loader=lambda code, days: (_history_df(), "fake"),
                ).run_once(current_time=datetime(2026, 5, 31, 10, 0))

            self.assertEqual(artifacts.trade_date, effective_date)
            self.assertIn("2026-05-29", str(artifacts.snapshot_file))
            self.assertIn("2026-05-29", str(artifacts.meta_file))

    def test_run_once_can_llm_review_top_recommendations_without_rescoring(self) -> None:
        universe = [
            StockUniverseItem(
                code=f"{600100 + idx}.SH",
                display_code=f"{600100 + idx}",
                name=f"复核股票{idx}",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            )
            for idx in range(12)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                recommendation_output_dir=temp_dir,
                recommendation_market="cn",
                recommendation_profile="beginner_cn",
                recommendation_profile_path="config/recommendation_profiles/beginner_cn.toml",
            )

            def fake_analysis_runner(code: str):
                return {
                    "query_id": f"review-{code}",
                    "report": {
                        "summary": {
                            "operation_advice": "谨慎观望，等待确认",
                            "trend_prediction": "震荡",
                            "sentiment_score": 48,
                        },
                        "details": {
                            "risk_warning": "短线波动风险较高",
                        },
                    },
                }

            with patch("src.services.recommendation_service.load_stock_universe", return_value=universe):
                artifacts = RecommendationService(
                    config=config,
                    snapshot_fetcher=_FakeSnapshotFetcher(),
                    history_loader=lambda code, days: (_history_df(), "fake"),
                    analysis_runner=fake_analysis_runner,
                ).run_once(run_deep_analysis=True)

            rows = RecommendationService._read_csv(artifacts.recommendations_file)
            self.assertGreaterEqual(len(rows), 1)
            self.assertEqual(rows[0]["llm_review_status"], "downgraded")
            self.assertTrue(rows[0]["analysis_query_id"].startswith("review-"))
            self.assertEqual(rows[0]["recommendation_label"], "只看不追")
            self.assertIn("LLM复核", rows[0]["negative_reasons"])
            self.assertEqual(artifacts.summary["deep_analyzed_count"], len(rows))

    def test_run_once_syncs_db_index_for_paginated_history(self) -> None:
        universe = [
            StockUniverseItem(
                code=f"{600200 + idx}.SH",
                display_code=f"{600200 + idx}",
                name=f"索引股票{idx}",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            )
            for idx in range(12)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "recommendation-index.db"
            DatabaseManager.reset_instance()
            db = DatabaseManager(f"sqlite:///{db_path}")
            config = SimpleNamespace(
                recommendation_output_dir=temp_dir,
                recommendation_market="cn",
                recommendation_profile="beginner_cn",
                recommendation_profile_path="config/recommendation_profiles/beginner_cn.toml",
                recommendation_db_index_enabled=True,
            )
            with patch("src.services.recommendation_service.load_stock_universe", return_value=universe):
                artifacts = RecommendationService(
                    config=config,
                    snapshot_fetcher=_FakeSnapshotFetcher(),
                    history_loader=lambda code, days: (_history_df(), "fake"),
                    db_manager=db,
                ).run_once()

            with db.get_session() as session:
                indexed = session.query(RecommendationRunIndex).filter(
                    RecommendationRunIndex.run_id == artifacts.run_id
                ).one()
                self.assertEqual(indexed.recommended_count, artifacts.summary["recommended_count"])

            page = RecommendationService(config=config, db_manager=db).list_runs_paginated(
                market="cn",
                page=1,
                limit=10,
                query="beginner_cn",
            )
            self.assertEqual(page["source"], "db")
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["run_id"], artifacts.run_id)

            run_detail = RecommendationService(config=config, db_manager=db).get_run(artifacts.run_id)
            self.assertIsNotNone(run_detail)
            self.assertIn("data_sources", run_detail["meta"])
            self.assertIn("filter_breakdown", run_detail["meta"]["summary"])
            DatabaseManager.reset_instance()

    def test_low_snapshot_coverage_writes_meta_without_recommendations(self) -> None:
        universe = [
            StockUniverseItem(
                code=f"{600000 + idx}.SH",
                display_code=f"{600000 + idx}",
                name=f"测试股票{idx}",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            )
            for idx in range(10)
        ]

        class LowCoverageFetcher:
            def fetch(self, *, trade_date, universe, preferred_sources):
                return [
                    MarketSnapshotItem(
                        trade_date=trade_date,
                        code=universe[0].display_code,
                        name=universe[0].name,
                        price=10,
                        volume=1,
                        amount=1,
                        source="fake",
                    )
                ], [{"name": "fake", "status": "ok", "rows": 1}]

        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                recommendation_output_dir=temp_dir,
                recommendation_market="cn",
                recommendation_profile="beginner_cn",
                recommendation_profile_path="config/recommendation_profiles/beginner_cn.toml",
            )
            with patch("src.services.recommendation_service.load_stock_universe", return_value=universe):
                artifacts = RecommendationService(
                    config=config,
                    snapshot_fetcher=LowCoverageFetcher(),
                    history_loader=lambda code, days: (_history_df(), "fake"),
                ).run_once()

            self.assertTrue(artifacts.meta_file.is_file())
            self.assertIsNone(artifacts.candidates_file)
            self.assertIsNone(artifacts.recommendations_file)
            self.assertTrue(artifacts.warnings)

    def test_retention_cleanup_removes_old_recommendation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_file = Path(temp_dir) / "runs" / "cn" / "2026-01-01" / "old.meta.json"
            old_file.parent.mkdir(parents=True, exist_ok=True)
            old_file.write_text("{}", encoding="utf-8")
            old_time = 1_700_000_000
            os.utime(old_file, (old_time, old_time))

            config = SimpleNamespace(
                recommendation_output_dir=temp_dir,
                recommendation_snapshot_retention_days=1,
            )
            RecommendationService(config=config)._cleanup_retention()

            self.assertFalse(old_file.exists())

    def test_run_backtest_writes_file_with_run_and_profile_identity(self) -> None:
        trade_date = date(2026, 5, 29)
        run_id = "cn-2026-05-29-153000-beginner_cn-test"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_file = root / "snapshots" / "cn" / f"{trade_date.isoformat()}.market.csv"
            recommendations_file = root / "runs" / "cn" / trade_date.isoformat() / "153000.recommendations.csv"
            meta_file = root / "runs" / "cn" / trade_date.isoformat() / "153000.meta.json"
            _write_csv(snapshot_file, [{
                "trade_date": trade_date.isoformat(),
                "code": "600519",
                "name": "贵州茅台",
                "price": 100,
                "change_pct": 1,
                "change_amount": 1,
                "volume": 100,
                "amount": 100000000,
                "volume_ratio": 1,
                "turnover_rate": 1,
                "amplitude": 2,
                "open": 99,
                "high": 101,
                "low": 98,
                "pre_close": 99,
                "pe_ratio": "",
                "pb_ratio": "",
                "total_mv": "",
                "circ_mv": "",
                "change_60d": "",
                "high_52w": "",
                "low_52w": "",
                "source": "fake",
                "data_quality": "ok",
            }])
            _write_csv(recommendations_file, [{
                "trade_date": trade_date.isoformat(),
                "code": "600519",
                "name": "贵州茅台",
                "strategy": "trend_pullback",
                "passed_hard_filters": "True",
                "filtered_by": "",
                "selection_score": "88",
                "beginner_safety_score": "80",
                "entry_quality_score": "0.8",
                "liquidity_score": "0.8",
                "trend_score": "0.8",
                "volume_price_score": "0.8",
                "sector_score": "0.5",
                "stability_score": "0.8",
                "risk_penalty": "0",
                "risk_tags": "",
                "positive_reasons": "趋势良好",
                "negative_reasons": "",
                "watch_price": "99",
                "stop_loss": "96",
                "take_profit_reference": "108",
                "rank": "1",
                "recommendation_level": "focus",
                "recommendation_label": "重点关注",
                "beginner_action": "观察",
                "no_position_action": "等待",
                "has_position_action": "持有",
                "llm_review_status": "not_run",
                "analysis_query_id": "",
            }])
            meta_file.parent.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps({
                "schema_version": 1,
                "run_id": run_id,
                "market": "cn",
                "trade_date": trade_date.isoformat(),
                "profile_hash": "sha256:test",
                "snapshot_file": str(snapshot_file.relative_to(root)),
                "recommendations_file": str(recommendations_file.relative_to(root)),
                "summary": {"recommended_count": 1},
                "warnings": [],
            }), encoding="utf-8")
            config = SimpleNamespace(recommendation_output_dir=temp_dir)
            service = RecommendationService(
                config=config,
                history_loader=lambda code, days: (_future_history_df(trade_date, 100), "fake"),
            )

            summary = service.run_backtest(run_id, windows=(3, 5))
            backtest_file = service.get_run_file(run_id, "backtest")
            rows = RecommendationService._read_csv(backtest_file)

            self.assertEqual(summary["processed"], 1)
            self.assertTrue(backtest_file and backtest_file.is_file())
            self.assertEqual(rows[0]["run_id"], run_id)
            self.assertEqual(rows[0]["profile_hash"], "sha256:test")
            self.assertEqual(rows[0]["status_3d"], "completed")
            self.assertIn("backtest_files", json.loads(meta_file.read_text(encoding="utf-8")))

    def test_feature_hard_filters_exclude_low_circulating_market_value(self) -> None:
        profile = load_recommendation_profile("beginner_cn")
        universe = [
            StockUniverseItem(
                code="600001.SH",
                display_code="600001",
                name="测试股票1",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            ),
            StockUniverseItem(
                code="600002.SH",
                display_code="600002",
                name="测试股票2",
                market="CN",
                asset_type="stock",
                active=True,
                board="main",
            ),
        ]
        snapshot = [
            MarketSnapshotItem(
                trade_date=date(2026, 5, 29),
                code="600001",
                name="测试股票1",
                price=12,
                change_pct=1.0,
                volume=15000000,
                amount=300000000,
                volume_ratio=1.0,
                turnover_rate=3.0,
                amplitude=3.0,
                circ_mv=1000000000,
            ),
            MarketSnapshotItem(
                trade_date=date(2026, 5, 29),
                code="600002",
                name="测试股票2",
                price=12,
                change_pct=1.0,
                volume=15000000,
                amount=300000000,
                volume_ratio=1.0,
                turnover_rate=3.0,
                amplitude=3.0,
                circ_mv=8000000000,
            ),
        ]

        result = RecommendationScreener(
            profile,
            history_loader=lambda code, days: (_history_df(), "fake"),
        ).screen(
            trade_date=date(2026, 5, 29),
            universe=universe,
            snapshot=snapshot,
        )

        self.assertNotIn("600001", {candidate.code for candidate in result.candidates})
        self.assertIn("600002", {candidate.code for candidate in result.candidates})
        self.assertGreaterEqual(result.filtered_count, 1)
        self.assertEqual(result.filter_breakdown.get("circ_mv_too_low"), 1)

    def test_latest_endpoint_returns_persisted_recommendations(self) -> None:
        fake_payload = {
            "meta": {"run_id": "run-1"},
            "recommendations": [{"code": "600519", "recommendation_label": "重点关注"}],
        }
        service = SimpleNamespace(latest=lambda market="cn": fake_payload)
        with patch.object(recommendations_endpoint, "RecommendationService", return_value=service):
            response = recommendations_endpoint.get_latest_recommendations(
                market="cn",
                config=SimpleNamespace(),
            )

        self.assertEqual(response["meta"]["run_id"], "run-1")
        self.assertEqual(response["recommendations"][0]["code"], "600519")


if __name__ == "__main__":
    unittest.main()

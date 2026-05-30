import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { recommendationsApi } from '../../api/recommendations';
import RecommendationsPage from '../RecommendationsPage';

vi.mock('../../api/recommendations', () => ({
  recommendationsApi: {
    listRuns: vi.fn(),
    getRun: vi.fn(),
    runBacktest: vi.fn(),
    fileUrl: vi.fn((runId: string, kind: string) => `/download/${runId}/${kind}`),
  },
}));

describe('RecommendationsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(recommendationsApi.listRuns).mockResolvedValue({
      items: [
        {
          runId: 'cn-2026-05-29-153000-beginner',
          market: 'cn',
          tradeDate: '2026-05-29',
          generatedAt: '2026-05-29T15:30:00+08:00',
          profile: 'beginner_cn',
          summary: {
            recommendedCount: 1,
            scoredCount: 20,
            snapshotCount: 5000,
          },
        },
      ],
      total: 1,
      page: 1,
      limit: 20,
      source: 'db',
    });
    vi.mocked(recommendationsApi.getRun).mockResolvedValue({
      meta: {
        runId: 'cn-2026-05-29-153000-beginner',
        tradeDate: '2026-05-29',
        generatedAt: '2026-05-29T15:30:00+08:00',
      },
      recommendations: [
        {
          code: '600519',
          name: '贵州茅台',
          rank: 1,
          strategy: 'trend_pullback',
          selectionScore: 88,
          llmReviewStatus: 'passed',
          analysisQueryId: 'review-1',
          watchPrice: 100,
          stopLoss: 95,
          positiveReasons: 'MA20 斜率向上;成交额充足',
        },
      ],
    });
    vi.mocked(recommendationsApi.runBacktest).mockResolvedValue({
      runId: 'cn-2026-05-29-153000-beginner',
      market: 'cn',
      windows: [3, 5, 10, 20],
      processed: 1,
      completed: 1,
      insufficient: 0,
      errors: 0,
      backtestFile: 'runs/cn/2026-05-29/153000.backtest.csv',
    });
  });

  it('renders recommendation runs, detail table, downloads and backtest action', async () => {
    render(
      <MemoryRouter>
        <RecommendationsPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('盘后推荐历史')).toBeInTheDocument();
    expect(await screen.findByText('2026-05-29')).toBeInTheDocument();
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('trend_pullback')).toBeInTheDocument();
    expect(screen.getByText('review-1')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /recommendations/ })).toHaveAttribute(
      'href',
      '/download/cn-2026-05-29-153000-beginner/recommendations',
    );

    fireEvent.click(screen.getByRole('button', { name: /运行回测/ }));

    await waitFor(() => {
      expect(recommendationsApi.runBacktest).toHaveBeenCalledWith('cn-2026-05-29-153000-beginner');
    });
    expect(await screen.findByText(/推荐回测已生成/)).toBeInTheDocument();
  });
});

import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, Play, RefreshCw, Search } from 'lucide-react';
import {
  AppPage,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  Input,
  PageHeader,
} from '../components/common';
import {
  recommendationsApi,
  type RecommendationBacktestResponse,
  type RecommendationItem,
  type RecommendationLatestResponse,
  type RecommendationRunListResponse,
} from '../api/recommendations';

const formatScore = (value?: string | number): string => {
  if (value === undefined || value === null || value === '') return '-';
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(1) : String(value);
};

const splitReasons = (value?: string): string[] => (
  (value || '').split(';').map((item) => item.trim()).filter(Boolean)
);

const fileKinds = ['recommendations', 'candidates', 'market', 'meta', 'profile', 'backtest'] as const;

type RunItem = RecommendationRunListResponse['items'][number];

const runIdOf = (run: RunItem): string => run.runId || '';

const RecommendationTable: React.FC<{ items: RecommendationItem[] }> = ({ items }) => {
  if (!items.length) {
    return <EmptyState title="暂无推荐明细" description="该运行没有生成最终推荐，或推荐文件尚不可用。" />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-left text-sm">
        <thead className="border-b border-subtle text-xs text-muted-text">
          <tr>
            <th className="px-3 py-2 font-medium">排序</th>
            <th className="px-3 py-2 font-medium">股票</th>
            <th className="px-3 py-2 font-medium">策略</th>
            <th className="px-3 py-2 font-medium">分数</th>
            <th className="px-3 py-2 font-medium">复核</th>
            <th className="px-3 py-2 font-medium">关注/止损</th>
            <th className="px-3 py-2 font-medium">理由</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-subtle">
          {items.map((item, index) => (
            <tr key={`${item.code}-${index}`} className="align-top">
              <td className="px-3 py-2 text-muted-text">#{item.rank || index + 1}</td>
              <td className="px-3 py-2">
                <div className="font-medium text-foreground">{item.name}</div>
                <div className="text-xs text-muted-text">{item.code}</div>
              </td>
              <td className="px-3 py-2 text-secondary-text">{item.strategy || '-'}</td>
              <td className="px-3 py-2 text-secondary-text">{formatScore(item.selectionScore)}</td>
              <td className="px-3 py-2 text-secondary-text">
                {item.llmReviewStatus && item.llmReviewStatus !== 'not_run' ? item.llmReviewStatus : '-'}
                {item.analysisQueryId ? <div className="text-xs text-muted-text">{item.analysisQueryId}</div> : null}
              </td>
              <td className="px-3 py-2 text-secondary-text">
                <div>{item.watchPrice || '-'}</div>
                <div className="text-xs text-muted-text">{item.stopLoss || '-'}</div>
              </td>
              <td className="px-3 py-2 text-secondary-text">
                {splitReasons(item.positiveReasons).slice(0, 2).join('；') || '-'}
                {splitReasons(item.negativeReasons).some((reason) => reason.includes('LLM复核')) ? (
                  <div className="mt-1 text-xs text-warning">
                    {splitReasons(item.negativeReasons).filter((reason) => reason.includes('LLM复核')).join('；')}
                  </div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const RecommendationsPage: React.FC = () => {
  const [runs, setRuns] = useState<RecommendationRunListResponse | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string>('');
  const [detail, setDetail] = useState<RecommendationLatestResponse | null>(null);
  const [query, setQuery] = useState('');
  const [isLoadingRuns, setIsLoadingRuns] = useState(false);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [isRunningBacktest, setIsRunningBacktest] = useState(false);
  const [backtestResult, setBacktestResult] = useState<RecommendationBacktestResponse | null>(null);
  const [error, setError] = useState('');

  const selectedRun = useMemo(
    () => runs?.items.find((item) => runIdOf(item) === selectedRunId),
    [runs?.items, selectedRunId],
  );

  const loadRuns = useCallback(async (search = query) => {
    setIsLoadingRuns(true);
    setError('');
    try {
      const response = await recommendationsApi.listRuns({ page: 1, limit: 20, query: search || undefined });
      setRuns(response);
      const firstRunId = response.items[0] ? runIdOf(response.items[0]) : '';
      setSelectedRunId((current) => current || firstRunId);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '推荐历史加载失败');
    } finally {
      setIsLoadingRuns(false);
    }
  }, [query]);

  const loadDetail = useCallback(async (runId: string) => {
    if (!runId) {
      setDetail(null);
      return;
    }
    setIsLoadingDetail(true);
    setError('');
    try {
      const response = await recommendationsApi.getRun(runId);
      setDetail(response);
      setBacktestResult(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '推荐详情加载失败');
    } finally {
      setIsLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    void loadRuns('');
  }, [loadRuns]);

  useEffect(() => {
    void loadDetail(selectedRunId);
  }, [loadDetail, selectedRunId]);

  const handleSearch = () => {
    setSelectedRunId('');
    void loadRuns(query);
  };

  const handleBacktest = async () => {
    if (!selectedRunId) return;
    setIsRunningBacktest(true);
    setError('');
    try {
      const response = await recommendationsApi.runBacktest(selectedRunId);
      setBacktestResult(response);
      await loadRuns(query);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '推荐回测失败');
    } finally {
      setIsRunningBacktest(false);
    }
  };

  return (
    <AppPage>
      <div className="space-y-4">
        <PageHeader
          eyebrow="Recommendations"
          title="盘后推荐历史"
          description="查看每日全市场选股推荐、下载 CSV 审计文件，并对历史推荐运行回测。"
          actions={(
            <Button type="button" variant="secondary" size="sm" onClick={() => void loadRuns(query)} isLoading={isLoadingRuns}>
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              刷新
            </Button>
          )}
        />

        {error ? <InlineAlert variant="danger" title="操作失败" message={error} /> : null}

        <div className="grid gap-4 lg:grid-cols-[22rem,minmax(0,1fr)]">
          <Card padding="md" className="space-y-3">
            <div className="flex gap-2">
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索 run/profile"
                aria-label="搜索推荐运行"
              />
              <Button type="button" variant="secondary" size="md" onClick={handleSearch}>
                <Search className="h-4 w-4" aria-hidden="true" />
              </Button>
            </div>

            <div className="space-y-2">
              {runs?.items.length ? runs.items.map((run) => {
                const runId = runIdOf(run);
                const selected = runId === selectedRunId;
                return (
                  <button
                    key={runId}
                    type="button"
                    onClick={() => setSelectedRunId(runId)}
                    className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                      selected ? 'border-primary/40 bg-primary/10' : 'border-subtle bg-background/40 hover:bg-hover'
                    }`}
                  >
                    <div className="font-medium text-foreground">{run.tradeDate || '-'}</div>
                    <div className="mt-1 truncate text-xs text-muted-text">{runId}</div>
                    <div className="mt-1 text-xs text-secondary-text">
                      推荐 {run.summary?.recommendedCount ?? '-'} · 候选 {run.summary?.scoredCount ?? '-'}
                    </div>
                  </button>
                );
              }) : (
                <EmptyState title="暂无推荐运行" description={isLoadingRuns ? '正在读取推荐历史...' : '运行一次盘后推荐后，这里会显示历史。'} />
              )}
            </div>
          </Card>

          <Card padding="md" className="space-y-4">
            {selectedRun ? (
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <h2 className="text-lg font-semibold text-foreground">{selectedRun.tradeDate} 推荐详情</h2>
                  <p className="mt-1 text-xs text-muted-text">{selectedRunId}</p>
                  <p className="mt-2 text-sm text-secondary-text">
                    推荐 {selectedRun.summary?.recommendedCount ?? '-'} · 评分候选 {selectedRun.summary?.scoredCount ?? '-'} ·
                    快照 {selectedRun.summary?.snapshotCount ?? '-'}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" variant="secondary" size="sm" isLoading={isRunningBacktest} onClick={handleBacktest}>
                    <Play className="h-4 w-4" aria-hidden="true" />
                    运行回测
                  </Button>
                  {fileKinds.map((kind) => (
                    <a
                      key={kind}
                      href={recommendationsApi.fileUrl(selectedRunId, kind)}
                      className="inline-flex h-9 items-center justify-center gap-1 rounded-xl border border-border/70 bg-card px-3 text-xs text-foreground transition-colors hover:bg-hover"
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden="true" />
                      {kind}
                    </a>
                  ))}
                </div>
              </div>
            ) : null}

            {backtestResult ? (
              <InlineAlert
                variant="success"
                title="推荐回测已生成"
                message={`处理 ${backtestResult.processed} 条，完成 ${backtestResult.completed} 条，文件：${backtestResult.backtestFile}`}
              />
            ) : null}

            {isLoadingDetail ? (
              <EmptyState title="正在读取推荐详情" />
            ) : (
              <RecommendationTable items={detail?.recommendations || []} />
            )}
          </Card>
        </div>
      </div>
    </AppPage>
  );
};

export default RecommendationsPage;

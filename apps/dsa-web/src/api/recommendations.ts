import apiClient from './index';
import { toCamelCase } from './utils';

export type RecommendationItem = {
  tradeDate?: string;
  code: string;
  name: string;
  strategy?: string;
  selectionScore?: string | number;
  recommendationLevel?: string;
  recommendationLabel?: string;
  llmReviewStatus?: string;
  analysisQueryId?: string;
  beginnerAction?: string;
  noPositionAction?: string;
  hasPositionAction?: string;
  positiveReasons?: string;
  negativeReasons?: string;
  riskTags?: string;
  watchPrice?: string | number;
  stopLoss?: string | number;
  rank?: string | number;
};

export type RecommendationLatestResponse = {
  meta: {
    runId: string;
    tradeDate: string;
    generatedAt: string;
    profile?: string;
    summary?: {
      recommendedCount?: number;
      scoredCount?: number;
      snapshotCount?: number;
      universeCount?: number;
      deepAnalyzedCount?: number;
      coverageRatio?: number;
    };
    warnings?: string[];
  };
  recommendations: RecommendationItem[];
  candidates?: RecommendationItem[];
};

export type RecommendationRunResponse = {
  runId: string;
  market: string;
  tradeDate: string;
  generatedAt: string;
  summary: Record<string, unknown>;
  warnings: string[];
};

export type RecommendationRunListResponse = {
  items: Array<RecommendationLatestResponse['meta'] & {
    runId?: string;
    market?: string;
    source?: string;
  }>;
  total: number;
  page: number;
  limit: number;
  source?: string;
};

export type RecommendationBacktestResponse = {
  runId: string;
  market: string;
  windows: number[];
  processed: number;
  completed: number;
  insufficient: number;
  errors: number;
  backtestFile: string;
};

export const recommendationsApi = {
  getLatest: async (): Promise<RecommendationLatestResponse | null> => {
    try {
      const response = await apiClient.get<Record<string, unknown>>('/api/v1/recommendations/latest', {
        validateStatus: (status) => status === 200 || status === 404,
      });
      if (response.status === 404) {
        return null;
      }
      return toCamelCase<RecommendationLatestResponse>(response.data);
    } catch {
      return null;
    }
  },

  run: async (forceRefreshSnapshot = false, runDeepAnalysis?: boolean): Promise<RecommendationRunResponse> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/recommendations/run', undefined, {
      params: {
        force_refresh_snapshot: forceRefreshSnapshot,
        ...(runDeepAnalysis === undefined ? {} : { run_deep_analysis: runDeepAnalysis }),
      },
    });
    return toCamelCase<RecommendationRunResponse>(response.data);
  },

  listRuns: async (params?: {
    market?: string;
    page?: number;
    limit?: number;
    query?: string;
  }): Promise<RecommendationRunListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/recommendations/runs', { params });
    return toCamelCase<RecommendationRunListResponse>(response.data);
  },

  getRun: async (runId: string, market = 'cn'): Promise<RecommendationLatestResponse> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/recommendations/runs/${runId}`, {
      params: { market },
    });
    return toCamelCase<RecommendationLatestResponse>(response.data);
  },

  runBacktest: async (runId: string, windows = '3,5,10,20', market = 'cn'): Promise<RecommendationBacktestResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(`/api/v1/recommendations/runs/${runId}/backtest`, undefined, {
      params: { market, windows },
    });
    return toCamelCase<RecommendationBacktestResponse>(response.data);
  },

  fileUrl: (runId: string, kind: string, market = 'cn'): string => {
    const baseUrl = apiClient.defaults.baseURL || '';
    const params = new URLSearchParams({ market });
    return `${baseUrl}/api/v1/recommendations/runs/${encodeURIComponent(runId)}/files/${encodeURIComponent(kind)}?${params.toString()}`;
  },
};

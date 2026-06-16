import type { GraphData, GraphStats, KnowledgeChainReport, NodeDetail, SearchResult, QueryResult } from './types';

const BASE_URL = '/api';

// Bearer token for API authentication (injected at build or runtime)
const API_TOKEN = import.meta.env.VITE_API_TOKEN ?? '';

interface FetchOptions {
  method?: 'GET' | 'POST';
  body?: Record<string, unknown>;
}

async function fetchJson<T>(url: string, options?: FetchOptions): Promise<T> {
  const headers: Record<string, string> = {};
  if (API_TOKEN) {
    headers['Authorization'] = `Bearer ${API_TOKEN}`;
  }
  const init: RequestInit = { headers };
  if (options?.method === 'POST') {
    headers['Content-Type'] = 'application/json';
    init.method = 'POST';
    if (options.body) {
      init.body = JSON.stringify(options.body);
    }
  }
  const res = await fetch(`${BASE_URL}${url}`, init);
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

export const api = {
  getGraphData: () => fetchJson<GraphData>('/graph/data'),

  getGraphStats: () => fetchJson<GraphStats>('/graph/stats'),

  searchEntities: (q: string) => fetchJson<{ results: SearchResult[] }>(`/graph/search?q=${encodeURIComponent(q)}`),

  getNodeDetail: (nodeId: string) => fetchJson<NodeDetail>(`/graph/node/${encodeURIComponent(nodeId)}`),

  getNodeReport: (nodeId: string) => fetchJson<KnowledgeChainReport>(`/graph/node-report/${encodeURIComponent(nodeId)}`),

  getNeighbors: (nodeId: string, depth = 2) =>
    fetchJson<GraphData>(`/graph/neighbors/${encodeURIComponent(nodeId)}?depth=${depth}`),

  getPath: (fromId: string, toId: string) =>
    fetchJson<{ nodes: { id: string; name: string }[]; edges: { from: string; to: string; type: string }[] }>(
      `/graph/path?from_id=${encodeURIComponent(fromId)}&to_id=${encodeURIComponent(toId)}`
    ),

  query: (question: string) =>
    fetchJson<QueryResult>('/query', { method: 'POST', body: { question } }),
};

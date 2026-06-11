import type { GraphData, GraphStats, KnowledgeChainReport, NodeDetail, SearchResult } from './types';

const BASE_URL = '/api';

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${url}`);
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
};

import type { GraphData, GraphStats, KnowledgeChainReport, NodeDetail, SearchResult, QueryResult } from './types';

const BASE_URL = '/api';
const ACCESS_KEY = 'kb_access_token';
const REFRESH_KEY = 'kb_refresh_token';

function getAuthToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

async function refreshAccessToken(): Promise<string | null> {
  const refresh = localStorage.getItem(REFRESH_KEY);
  if (!refresh) return null;
  try {
    const res = await fetch(`${BASE_URL}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    localStorage.setItem(ACCESS_KEY, data.access_token);
    localStorage.setItem(REFRESH_KEY, data.refresh_token);
    return data.access_token;
  } catch {
    return null;
  }
}

interface FetchOptions {
  method?: 'GET' | 'POST';
  body?: Record<string, unknown>;
}

async function fetchJson<T>(url: string, options?: FetchOptions): Promise<T> {
  const headers: Record<string, string> = {};

  if (options?.method === 'POST') {
    headers['Content-Type'] = 'application/json';
  }

  const doFetch = (token: string | null): RequestInit => {
    const h = { ...headers };
    if (token) h['Authorization'] = `Bearer ${token}`;
    const init: RequestInit = { headers: h };
    if (options?.method === 'POST') {
      init.method = 'POST';
      if (options.body) {
        init.body = JSON.stringify(options.body);
      }
    }
    return init;
  };

  let token = getAuthToken();
  let res = await fetch(`${BASE_URL}${url}`, doFetch(token));

  // Handle 401: try refresh if we have a token, otherwise redirect to login
  if (res.status === 401) {
    if (token) {
      // Token might be expired — try refreshing
      const newToken = await refreshAccessToken();
      if (newToken) {
        token = newToken;
        res = await fetch(`${BASE_URL}${url}`, doFetch(token));
      } else {
        // Refresh failed — clear tokens and redirect to login
        localStorage.removeItem(ACCESS_KEY);
        localStorage.removeItem(REFRESH_KEY);
        window.location.reload();
        throw new Error('Session expired');
      }
    } else {
      // No token at all — shouldn't happen (ProtectedRoute guards this),
      // but as a safety net, redirect to login
      window.location.href = '/ui/login';
      throw new Error('Not authenticated');
    }
  }

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

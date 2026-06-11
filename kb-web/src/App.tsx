import { useState, useEffect, useCallback } from 'react';
import GraphCanvas from './components/graph/GraphCanvas';
import NodeDetailPanel from './components/panel/NodeDetailPanel';
import StatsPanel from './components/panel/StatsPanel';
import SearchBar from './components/search/SearchBar';
import { api } from './api/client';
import type { GraphData, GraphStats } from './api/types';

export default function App() {
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [highlightNodeId, setHighlightNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Load graph data and stats on mount
  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        const [data, statsData] = await Promise.all([
          api.getGraphData(),
          api.getGraphStats(),
        ]);
        setGraphData(data);
        setStats(statsData);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load graph data');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  // Handle node click → open report panel
  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedNodeId(nodeId);
  }, []);

  // Handle node double click → expand neighborhood (future enhancement)
  const handleNodeDoubleClick = useCallback((nodeId: string) => {
    console.log('Double click expand neighborhood:', nodeId);
  }, []);

  // Handle search select → highlight node + open panel
  const handleSearchSelect = useCallback((nodeId: string) => {
    setHighlightNodeId(nodeId);
    setSelectedNodeId(nodeId);
  }, []);

  // Handle close panel
  const handleClosePanel = useCallback(() => {
    setSelectedNodeId(null);
    setHighlightNodeId(null);
  }, []);

  return (
    <div className="w-screen h-screen flex flex-col bg-[#0f172a]">
      {/* Top Bar */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-[#334155] bg-[#1e293b] shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#06b6d4] to-[#8b5cf6] flex items-center justify-center">
            <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <h1 className="text-base font-semibold text-[#f1f5f9]">Knowledge Graph Visualizer</h1>
        </div>
        <SearchBar onSelectNode={handleSearchSelect} />
      </header>

      {/* Main Content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Sidebar - Stats */}
        <aside className="w-[220px] border-r border-[#334155] bg-[#0f172a] overflow-y-auto shrink-0">
          <StatsPanel stats={stats} />
        </aside>

        {/* Center - Graph Canvas */}
        <main className="flex-1 relative">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-[#0f172a] z-10">
              <div className="flex flex-col items-center gap-3">
                <div className="w-10 h-10 border-3 border-[#06b6d4] border-t-transparent rounded-full animate-spin" />
                <span className="text-sm text-[#64748b]">加载知识图谱...</span>
              </div>
            </div>
          )}

          {error && (
            <div className="absolute inset-0 flex items-center justify-center bg-[#0f172a] z-10">
              <div className="text-center">
                <div className="text-[#ef4444] text-sm mb-2">加载失败</div>
                <div className="text-[#64748b] text-xs">{error}</div>
                <button
                  onClick={() => window.location.reload()}
                  className="mt-4 px-4 py-2 bg-[#334155] text-[#e2e8f0] rounded-lg text-sm hover:bg-[#475569] transition-colors"
                >
                  重试
                </button>
              </div>
            </div>
          )}

          <GraphCanvas
            data={graphData}
            onNodeClick={handleNodeClick}
            onNodeDoubleClick={handleNodeDoubleClick}
            highlightNodeId={highlightNodeId}
          />
        </main>

        {/* Right Sidebar - Node Detail / Knowledge Report */}
        {selectedNodeId && (
          <aside className="w-[380px] shrink-0">
            <NodeDetailPanel nodeId={selectedNodeId} onClose={handleClosePanel} />
          </aside>
        )}
      </div>
    </div>
  );
}

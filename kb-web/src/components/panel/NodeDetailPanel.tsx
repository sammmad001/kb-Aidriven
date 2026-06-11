import { useState, useEffect } from 'react';
import { api } from '../../api/client';
import type { KnowledgeChainReport } from '../../api/types';
import KnowledgeReport from './KnowledgeReport';

interface NodeDetailPanelProps {
  nodeId: string | null;
  onClose: () => void;
}

export default function NodeDetailPanel({ nodeId, onClose }: NodeDetailPanelProps) {
  const [report, setReport] = useState<KnowledgeChainReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!nodeId) {
      setReport(null);
      return;
    }

    setLoading(true);
    setError(null);

    api.getNodeReport(nodeId)
      .then(setReport)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [nodeId]);

  if (!nodeId) return null;

  return (
    <div className="h-full flex flex-col bg-[#0f172a] border-l border-[#334155]">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#334155] bg-[#1e293b]">
        <h2 className="text-sm font-semibold text-[#f1f5f9] m-0">知识链路报告</h2>
        <button
          onClick={onClose}
          className="text-[#64748b] hover:text-[#e2e8f0] transition-colors text-lg leading-none"
        >
          ×
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="flex items-center justify-center h-32">
            <div className="flex items-center gap-3 text-[#64748b]">
              <div className="w-5 h-5 border-2 border-[#06b6d4] border-t-transparent rounded-full animate-spin" />
              <span className="text-sm">生成知识链路报告...</span>
            </div>
          </div>
        )}

        {error && (
          <div className="p-4">
            <div className="text-sm text-[#ef4444] bg-[#ef444420] rounded-lg p-3">
              加载失败: {error}
            </div>
          </div>
        )}

        {report && <KnowledgeReport report={report} />}
      </div>
    </div>
  );
}

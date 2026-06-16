import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { KnowledgeChainReport } from '../api/types';
import KnowledgeReport from '../components/panel/KnowledgeReport';
import { useCopyToClipboard } from '../hooks/useCopyToClipboard';

export default function NodeDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [report, setReport] = useState<KnowledgeChainReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { copied, copy } = useCopyToClipboard();

  useEffect(() => {
    if (!id) return;
    const currentId = id;

    let cancelled = false;

    async function load() {
      setLoading(true);
      try {
        const r = await api.getNodeReport(currentId);
        if (!cancelled) setReport(r);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => { cancelled = true; };
  }, [id]);

  if (!id) {
    return (
      <div className="flex-1 flex items-center justify-center text-[#64748b]">
        缺少节点 ID
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-[#334155] bg-[#1e293b] shrink-0">
        <button
          onClick={() => navigate(-1)}
          className="flex items-center gap-2 text-sm text-[#94a3b8] hover:text-[#e2e8f0] transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          返回
        </button>
        <button
          onClick={() => copy(window.location.href)}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs text-[#94a3b8] hover:text-[#e2e8f0] hover:bg-[#334155] transition-colors"
        >
          {copied ? (
            <>
              <svg className="w-4 h-4 text-[#10b981]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              <span className="text-[#10b981]">已复制</span>
            </>
          ) : (
            <>
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
              </svg>
              复制链接
            </>
          )}
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="flex items-center justify-center h-40">
            <div className="flex items-center gap-3 text-[#64748b]">
              <div className="w-5 h-5 border-2 border-[#06b6d4] border-t-transparent rounded-full animate-spin" />
              <span className="text-sm">生成知识链路报告...</span>
            </div>
          </div>
        )}

        {error && (
          <div className="p-6">
            <div className="text-sm text-[#ef4444] bg-[#ef444420] rounded-lg p-4 max-w-2xl mx-auto">
              加载失败: {error}
            </div>
          </div>
        )}

        {report && (
          <div className="max-w-3xl mx-auto bg-[#0f172a]">
            <KnowledgeReport
              report={report}
              onNodeClick={(nodeId) => navigate(`/node/${nodeId}`)}
            />
          </div>
        )}
      </div>
    </div>
  );
}

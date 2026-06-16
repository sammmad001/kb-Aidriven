import ReactMarkdown from 'react-markdown';
import { useNavigate } from 'react-router-dom';
import type { QueryResult, QueryType } from '../../api/types';

interface QueryResultDisplayProps {
  result: QueryResult;
  question: string;
}

const QUERY_TYPE_LABELS: Record<QueryType, string> = {
  factual: '事实查询',
  relational: '关系查询',
  reasoning: '推理分析',
  global: '全局总结',
};

const QUERY_TYPE_COLORS: Record<QueryType, string> = {
  factual: '#06b6d4',
  relational: '#8b5cf6',
  reasoning: '#f59e0b',
  global: '#10b981',
};

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const color = pct >= 75 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-[#334155] rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <span className="text-xs text-[#64748b] shrink-0">{pct}%</span>
    </div>
  );
}

export default function QueryResultDisplay({ result, question }: QueryResultDisplayProps) {
  const navigate = useNavigate();
  const typeColor = QUERY_TYPE_COLORS[result.query_type] ?? '#64748b';

  return (
    <div className="max-w-3xl mx-auto space-y-4">
      {/* Question Echo */}
      <div className="flex items-start gap-3">
        <div className="shrink-0 w-8 h-8 rounded-lg bg-[#334155] flex items-center justify-center">
          <svg className="w-4 h-4 text-[#94a3b8]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
        </div>
        <div className="flex-1 pt-1">
          <p className="text-sm text-[#cbd5e1]">{question}</p>
        </div>
      </div>

      {/* Answer */}
      <div className="bg-[#1e293b] border border-[#334155] rounded-xl overflow-hidden">
        {/* Answer Header */}
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#334155] bg-[#0f172a]">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-md bg-gradient-to-br from-[#06b6d4] to-[#8b5cf6] flex items-center justify-center">
              <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
              </svg>
            </div>
            <span className="text-sm font-medium text-[#e2e8f0]">智能回答</span>
          </div>
          <span
            className="text-xs px-2 py-0.5 rounded-full"
            style={{ backgroundColor: `${typeColor}20`, color: typeColor }}
          >
            {QUERY_TYPE_LABELS[result.query_type] ?? result.query_type}
          </span>
        </div>

        {/* Answer Body */}
        <div className="qa-answer px-4 py-3 prose prose-invert prose-sm max-w-none text-sm text-[#cbd5e1]">
          <ReactMarkdown>{result.answer}</ReactMarkdown>
        </div>

        {/* Confidence + Depth */}
        <div className="px-4 py-2.5 border-t border-[#334155] bg-[#0f172a]">
          <div className="flex items-center gap-4">
            <div className="flex-1">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs text-[#64748b]">置信度</span>
              </div>
              <ConfidenceBar confidence={result.confidence} />
            </div>
            {result.depth > 0 && (
              <div className="shrink-0 text-center">
                <div className="text-lg font-bold text-[#8b5cf6]">{result.depth}</div>
                <div className="text-xs text-[#64748b]">推理深度</div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Source References */}
      {result.sources.length > 0 && (
        <div className="bg-[#1e293b] border border-[#334155] rounded-xl overflow-hidden">
          <div className="px-4 py-2.5 border-b border-[#334155] bg-[#0f172a]">
            <span className="text-sm font-medium text-[#e2e8f0]">
              引用来源
              <span className="ml-2 text-xs text-[#64748b]">{result.sources.length} 个节点</span>
            </span>
          </div>
          <div className="divide-y divide-[#334155]">
            {result.sources.map((src, i) => (
              <div key={i} className="flex items-center gap-3 px-4 py-2 hover:bg-[#0f172a] transition-colors">
                <span className="text-xs text-[#64748b] shrink-0 w-6">#{i + 1}</span>
                <button
                  onClick={() => navigate(`/node/${src.node_id}`)}
                  className="text-sm text-[#06b6d4] hover:underline truncate"
                >
                  {src.node_name}
                </button>
                <div className="flex-1" />
                <div className="flex items-center gap-2 shrink-0">
                  <div className="w-16 h-1 bg-[#334155] rounded-full overflow-hidden">
                    <div
                      className="h-full bg-[#06b6d4] rounded-full"
                      style={{ width: `${Math.round(src.relevance * 100)}%` }}
                    />
                  </div>
                  <span className="text-xs text-[#64748b] w-8 text-right">{Math.round(src.relevance * 100)}%</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Implicit Relations Used */}
      {result.implicit_relations_used.length > 0 && (
        <div className="bg-[#1e293b] border border-[#334155] rounded-xl overflow-hidden">
          <div className="px-4 py-2.5 border-b border-[#334155] bg-[#0f172a]">
            <span className="text-sm font-medium text-[#e2e8f0]">
              推理线索
              <span className="ml-2 text-xs text-[#64748b]">{result.implicit_relations_used.length} 条隐式关系</span>
            </span>
          </div>
          <div className="divide-y divide-[#334155]">
            {result.implicit_relations_used.map((rel, i) => (
              <div key={i} className="px-4 py-2.5">
                <div className="flex items-center gap-2 text-sm">
                  <span className="text-[#e2e8f0] font-medium">{rel.source}</span>
                  <svg className="w-3 h-3 text-[#8b5cf6] shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
                  </svg>
                  <span className="text-[#e2e8f0] font-medium">{rel.target}</span>
                  <span className="ml-auto text-xs px-1.5 py-0.5 rounded bg-[#8b5cf620] text-[#8b5cf6]">
                    {rel.type}
                  </span>
                  <span className="text-xs text-[#64748b]">{Math.round(rel.confidence * 100)}%</span>
                </div>
                {rel.evidence && (
                  <p className="text-xs text-[#64748b] mt-1 pl-1">{rel.evidence}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

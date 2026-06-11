import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import type { KnowledgeChainReport, RelationDetail, MultiHopPath } from '../../api/types';
import { IMPLICIT_TYPE_COLORS } from '../graph/graphStyles';

interface KnowledgeReportProps {
  report: KnowledgeChainReport;
}

function Section({ title, count, children, defaultOpen = true }: {
  title: string; count?: number; children: React.ReactNode; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-[#334155]">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-[#1e293b] transition-colors"
      >
        <span className="text-sm font-medium text-[#e2e8f0]">{title}</span>
        <span className="flex items-center gap-2">
          {count !== undefined && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-[#334155] text-[#94a3b8]">{count}</span>
          )}
          <span className="text-[#64748b] text-xs">{open ? '▲' : '▼'}</span>
        </span>
      </button>
      {open && <div className="px-4 pb-3">{children}</div>}
    </div>
  );
}

function RelationRow({ rel }: { rel: RelationDetail; focusNodeId: string }) {
  const isOutgoing = rel.direction === 'outgoing';
  const otherName = isOutgoing ? rel.target_name : rel.source_name;
  const isImplicit = rel.rel_type === 'IMPLICIT';
  const typeColor = rel.implicit_type ? IMPLICIT_TYPE_COLORS[rel.implicit_type] : '#64748b';

  return (
    <div className="flex items-start gap-2 py-1.5 text-xs">
      <span className="text-[#64748b] shrink-0">{isOutgoing ? '→' : '←'}</span>
      <span className="text-[#e2e8f0] font-medium">{otherName}</span>
      {isImplicit ? (
        <span className="px-1.5 py-0.5 rounded text-[10px]" style={{ backgroundColor: `${typeColor}20`, color: typeColor }}>
          {rel.implicit_type} {rel.confidence ? `${(rel.confidence * 100).toFixed(0)}%` : ''}
        </span>
      ) : (
        <span className="px-1.5 py-0.5 rounded bg-[#334155] text-[#94a3b8] text-[10px]">{rel.rel_type}</span>
      )}
      {rel.evidence && (
        <span className="text-[#64748b] truncate max-w-[120px]" title={rel.evidence}>{rel.evidence}</span>
      )}
    </div>
  );
}

function MultiHopRow({ path }: { path: MultiHopPath }) {
  return (
    <div className="py-1.5 text-xs">
      <div className="flex items-center gap-2">
        <span className="text-[#e2e8f0] font-medium">{path.target_name}</span>
        <span className="text-[#64748b]">{path.hop_count} 跳</span>
      </div>
      <div className="text-[#64748b] mt-0.5 truncate">
        {path.path_nodes.slice(0, 5).join(' → ')}
        {path.path_nodes.length > 5 && '...'}
      </div>
    </div>
  );
}

export default function KnowledgeReport({ report }: KnowledgeReportProps) {
  const { node, direct_relations, multi_hop_paths, implicit_relations, cluster_info, metrics } = report;

  return (
    <div className="text-sm">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[#334155] bg-[#1e293b]">
        <h3 className="text-base font-semibold text-[#f1f5f9] m-0">{node.name}</h3>
        <div className="flex items-center gap-2 mt-1">
          <span className="text-xs px-2 py-0.5 rounded bg-[#334155] text-[#06b6d4]">{node.node_type}</span>
          {node.tags.map((tag) => (
            <span key={tag} className="text-xs px-1.5 py-0.5 rounded bg-[#1e293b] text-[#94a3b8] border border-[#334155]">{tag}</span>
          ))}
        </div>
      </div>

      {/* Section 1: Overview */}
      <Section title="节点概况">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="text-[#64748b]">PageRank</div>
          <div className="text-[#e2e8f0]">{(node.page_rank ?? 0).toFixed(4)} <span className="text-[#64748b]">({String(metrics.rank_percent || '')})</span></div>
          <div className="text-[#64748b]">聚类</div>
          <div className="text-[#e2e8f0]">#{node.cluster_id} {cluster_info?.label || ''}</div>
          <div className="text-[#64748b]">入度 / 出度</div>
          <div className="text-[#e2e8f0]">{node.in_degree} / {node.out_degree}</div>
          {node.created_at && (
            <>
              <div className="text-[#64748b]">创建时间</div>
              <div className="text-[#e2e8f0]">{new Date(node.created_at).toLocaleDateString()}</div>
            </>
          )}
          {node.updated_at && (
            <>
              <div className="text-[#64748b]">更新时间</div>
              <div className="text-[#e2e8f0]">{new Date(node.updated_at).toLocaleDateString()}</div>
            </>
          )}
          <div className="text-[#64748b]">桥接状态</div>
          <div className="text-[#e2e8f0]">{metrics.is_bridge ? `连接 ${metrics.connected_clusters} 个聚类` : '非桥接'}</div>
        </div>
      </Section>

      {/* Section 2: Content */}
      {node.content && (
        <Section title="完整内容" defaultOpen={false}>
          <div className="prose prose-invert prose-sm max-w-none text-xs text-[#cbd5e1]">
            <ReactMarkdown>{node.content}</ReactMarkdown>
          </div>
        </Section>
      )}

      {/* Section 3: Direct Relations */}
      <Section title="直接关联（1跳）" count={direct_relations.length}>
        {direct_relations.length === 0 ? (
          <div className="text-xs text-[#64748b] py-2">无直接关系</div>
        ) : (
          <div className="max-h-[200px] overflow-y-auto">
            {direct_relations.map((rel, i) => (
              <RelationRow key={i} rel={rel} focusNodeId={node.id} />
            ))}
          </div>
        )}
      </Section>

      {/* Section 4: Multi-hop Paths */}
      <Section title="间接关联（多跳路径）" count={multi_hop_paths.length} defaultOpen={false}>
        {multi_hop_paths.length === 0 ? (
          <div className="text-xs text-[#64748b] py-2">无多跳路径</div>
        ) : (
          <div className="max-h-[200px] overflow-y-auto">
            {multi_hop_paths.slice(0, 15).map((path, i) => (
              <MultiHopRow key={i} path={path} />
            ))}
          </div>
        )}
      </Section>

      {/* Section 5: Implicit Relations */}
      <Section title="隐式关系推理" count={implicit_relations.length} defaultOpen={false}>
        {implicit_relations.length === 0 ? (
          <div className="text-xs text-[#64748b] py-2">无隐式关系</div>
        ) : (
          <div className="max-h-[200px] overflow-y-auto">
            {implicit_relations.map((rel, i) => (
              <RelationRow key={i} rel={rel} focusNodeId={node.id} />
            ))}
          </div>
        )}
      </Section>

      {/* Section 6: Network Metrics */}
      <Section title="知识网络指标">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="bg-[#1e293b] rounded-lg p-3 text-center">
            <div className="text-lg font-bold text-[#06b6d4]">{(node.page_rank ?? 0).toFixed(3)}</div>
            <div className="text-[#64748b] mt-1">PageRank</div>
          </div>
          <div className="bg-[#1e293b] rounded-lg p-3 text-center">
            <div className="text-lg font-bold text-[#8b5cf6]">{node.in_degree + node.out_degree}</div>
            <div className="text-[#64748b] mt-1">总连接数</div>
          </div>
          <div className="bg-[#1e293b] rounded-lg p-3 text-center">
            <div className="text-lg font-bold text-[#10b981]">{cluster_info?.node_count ?? 0}</div>
            <div className="text-[#64748b] mt-1">聚类规模</div>
          </div>
          <div className="bg-[#1e293b] rounded-lg p-3 text-center">
            <div className="text-lg font-bold text-[#f59e0b]">{String(metrics.connected_clusters ?? 0)}</div>
            <div className="text-[#64748b] mt-1">关联聚类</div>
          </div>
        </div>
      </Section>
    </div>
  );
}

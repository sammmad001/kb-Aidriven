import type { GraphStats } from '../../api/types';

interface StatsPanelProps {
  stats: GraphStats | null;
}

export default function StatsPanel({ stats }: StatsPanelProps) {
  if (!stats) {
    return (
      <div className="p-4 text-xs text-[#64748b]">
        加载中...
      </div>
    );
  }

  return (
    <div className="p-4 space-y-4">
      {/* Graph Stats */}
      <div>
        <h3 className="text-xs font-semibold text-[#64748b] uppercase tracking-wider mb-2">图谱统计</h3>
        <div className="space-y-2">
          <StatRow label="节点" value={stats.node_count} color="#06b6d4" />
          <StatRow label="边" value={stats.edge_count} color="#8b5cf6" />
          <StatRow label="聚类" value={stats.cluster_count} color="#f59e0b" />
          <StatRow label="实体" value={stats.entity_count} color="#10b981" />
          <StatRow label="概念" value={stats.concept_count} color="#ec4899" />
          <StatRow label="隐式边" value={stats.implicit_edge_count} color="#ef4444" />
        </div>
      </div>

      {/* Legend */}
      <div>
        <h3 className="text-xs font-semibold text-[#64748b] uppercase tracking-wider mb-2">图例</h3>
        <div className="space-y-1.5 text-xs">
          <div className="flex items-center gap-2">
            <div className="w-8 h-0.5 bg-[#475569]" />
            <span className="text-[#94a3b8]">显式关系</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-8 h-0.5 border-t-2 border-dashed border-[#3b82f6]" />
            <span className="text-[#94a3b8]">隐式关系</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-[#06b6d4] opacity-80" />
            <span className="text-[#94a3b8]">Entity</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rotate-45 bg-[#8b5cf6] opacity-80" />
            <span className="text-[#94a3b8]">Concept</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 bg-[#f59e0b] opacity-80" style={{ clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)' }} />
            <span className="text-[#94a3b8]">Comparison</span>
          </div>
        </div>
      </div>

      {/* Implicit Relation Types */}
      <div>
        <h3 className="text-xs font-semibold text-[#64748b] uppercase tracking-wider mb-2">隐式关系类型</h3>
        <div className="space-y-1.5 text-xs">
          <TypeRow color="#3b82f6" label="depends_on" desc="依赖" />
          <TypeRow color="#ef4444" label="trade_off" desc="权衡" />
          <TypeRow color="#f59e0b" label="bridges" desc="桥接" />
          <TypeRow color="#10b981" label="evolves_to" desc="演化" />
          <TypeRow color="#a855f7" label="solves" desc="解决" />
          <TypeRow color="#06b6d4" label="precedes" desc="先于" />
          <TypeRow color="#f43f5e" label="causes" desc="因果" />
          <TypeRow color="#eab308" label="contradicts" desc="矛盾" />
          <TypeRow color="#8b5cf6" label="analogous_to" desc="类比" />
        </div>
      </div>
    </div>
  );
}

function StatRow({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-[#94a3b8]">{label}</span>
      <span className="text-sm font-mono font-medium" style={{ color }}>{value}</span>
    </div>
  );
}

function TypeRow({ color, label, desc }: { color: string; label: string; desc: string }) {
  return (
    <div className="flex items-center gap-2">
      <div className="w-3 h-3 rounded-full shrink-0" style={{ backgroundColor: color }} />
      <span className="text-[#e2e8f0] font-mono text-[10px]">{label}</span>
      <span className="text-[#64748b]">{desc}</span>
    </div>
  );
}

import { useState } from 'react';
import type { QueryHistoryItem } from '../../api/types';

interface QueryHistoryProps {
  history: QueryHistoryItem[];
  onSelect: (item: QueryHistoryItem) => void;
  onRemove: (timestamp: number) => void;
  onClear: () => void;
}

const QUERY_TYPE_BADGE: Record<string, string> = {
  factual: '事实',
  relational: '关系',
  reasoning: '推理',
  global: '全局',
};

function formatTime(timestamp: number): string {
  const diff = Date.now() - timestamp;
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);

  if (days > 0) return `${days}天前`;
  if (hours > 0) return `${hours}小时前`;
  if (minutes > 0) return `${minutes}分钟前`;
  return '刚刚';
}

export default function QueryHistory({ history, onSelect, onRemove, onClear }: QueryHistoryProps) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#334155] shrink-0">
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="flex items-center gap-2 text-sm font-medium text-[#e2e8f0] hover:text-white transition-colors"
        >
          <svg
            className={`w-3 h-3 text-[#64748b] transition-transform ${collapsed ? '' : 'rotate-90'}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          查询历史
          {history.length > 0 && (
            <span className="text-xs px-1.5 py-0.5 rounded-full bg-[#334155] text-[#94a3b8]">{history.length}</span>
          )}
        </button>

        {history.length > 0 && (
          <button
            onClick={onClear}
            className="text-xs text-[#64748b] hover:text-[#ef4444] transition-colors"
          >
            清空
          </button>
        )}
      </div>

      {/* List */}
      {!collapsed && (
        <div className="flex-1 overflow-y-auto">
          {history.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <svg className="w-10 h-10 mx-auto text-[#334155] mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <p className="text-xs text-[#64748b]">暂无查询历史</p>
            </div>
          ) : (
            <div className="divide-y divide-[#1e293b]">
              {history.map((item) => (
                <div
                  key={item.timestamp}
                  className="group relative px-4 py-2.5 hover:bg-[#1e293b] transition-colors cursor-pointer"
                  onClick={() => onSelect(item)}
                >
                  {/* Remove button */}
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemove(item.timestamp);
                    }}
                    className="absolute right-2 top-2.5 opacity-0 group-hover:opacity-100 text-[#64748b] hover:text-[#ef4444] transition-all"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>

                  {/* Question */}
                  <p className="text-xs text-[#e2e8f0] pr-6 line-clamp-2 leading-relaxed">{item.question}</p>

                  {/* Meta */}
                  <div className="flex items-center gap-2 mt-1">
                    {item.query_type && (
                      <span className="text-[10px] px-1 py-0.5 rounded bg-[#334155] text-[#94a3b8]">
                        {QUERY_TYPE_BADGE[item.query_type] ?? item.query_type}
                      </span>
                    )}
                    <span className="text-[10px] text-[#64748b]">{formatTime(item.timestamp)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

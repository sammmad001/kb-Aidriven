import { useState, useCallback, useRef, useEffect } from 'react';
import { api } from '../../api/client';
import type { SearchResult } from '../../api/types';

interface SearchBarProps {
  onSelectNode: (nodeId: string) => void;
}

export default function SearchBar({ onSelectNode }: SearchBarProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(null);

  const doSearch = useCallback(async (q: string) => {
    if (q.length < 1) {
      setResults([]);
      setOpen(false);
      return;
    }
    setLoading(true);
    try {
      const data = await api.searchEntities(q);
      setResults(data.results);
      setOpen(true);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleChange = (value: string) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(value), 300);
  };

  const handleSelect = (nodeId: string) => {
    onSelectNode(nodeId);
    setOpen(false);
    setQuery('');
    setResults([]);
  };

  // Close dropdown on outside click
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div ref={containerRef} className="relative">
      <div className="flex items-center bg-[#1e293b] border border-[#334155] rounded-lg px-3 py-2 gap-2 w-[260px]">
        <svg className="w-4 h-4 text-[#64748b] shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <input
          type="text"
          value={query}
          onChange={(e) => handleChange(e.target.value)}
          placeholder="搜索知识节点..."
          className="bg-transparent border-none outline-none text-sm text-[#e2e8f0] placeholder-[#64748b] w-full"
        />
        {loading && (
          <div className="w-4 h-4 border-2 border-[#06b6d4] border-t-transparent rounded-full animate-spin shrink-0" />
        )}
      </div>

      {open && results.length > 0 && (
        <div className="absolute top-full mt-1 left-0 right-0 bg-[#1e293b] border border-[#334155] rounded-lg shadow-xl z-50 max-h-[300px] overflow-y-auto">
          {results.map((r) => (
            <button
              key={r.id}
              onClick={() => handleSelect(r.id)}
              className="w-full text-left px-3 py-2 hover:bg-[#334155] transition-colors border-b border-[#334155] last:border-b-0"
            >
              <div className="text-sm text-[#e2e8f0]">{r.name}</div>
              <div className="text-xs text-[#64748b] truncate mt-0.5">{r.summary}</div>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#334155] text-[#06b6d4]">{r.type}</span>
                <span className="text-[10px] text-[#64748b]">PR: {r.page_rank.toFixed(3)}</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

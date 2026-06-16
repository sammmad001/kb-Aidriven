import { useState, useCallback } from 'react';
import type { QueryHistoryItem } from '../api/types';

const STORAGE_KEY = 'kb-query-history';
const MAX_ITEMS = 50;

export function useQueryHistory() {
  const [history, setHistory] = useState<QueryHistoryItem[]>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  });

  const add = useCallback((item: Omit<QueryHistoryItem, 'timestamp'>) => {
    const newItem: QueryHistoryItem = { ...item, timestamp: Date.now() };
    setHistory((prev) => {
      const updated = [newItem, ...prev].slice(0, MAX_ITEMS);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(updated));
      } catch {
        // localStorage might be full or unavailable
      }
      return updated;
    });
  }, []);

  const remove = useCallback((timestamp: number) => {
    setHistory((prev) => {
      const updated = prev.filter((item) => item.timestamp !== timestamp);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(updated));
      } catch {
        // ignore
      }
      return updated;
    });
  }, []);

  const clear = useCallback(() => {
    setHistory([]);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
  }, []);

  return { history, add, remove, clear };
}

import { useState, useRef, useCallback, type KeyboardEvent } from 'react';

interface QueryInputProps {
  onSubmit: (question: string) => void;
  loading: boolean;
  placeholder: string;
  examples: string[];
}

export default function QueryInput({ onSubmit, loading, placeholder, examples }: QueryInputProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    onSubmit(trimmed);
    setText('');
  }, [text, loading, onSubmit]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  const handleExampleClick = useCallback(
    (example: string) => {
      if (loading) return;
      onSubmit(example);
    },
    [onSubmit, loading],
  );

  return (
    <div className="max-w-3xl mx-auto">
      {/* Input Row */}
      <div className="flex gap-2 items-end">
        <div className="flex-1 relative">
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            disabled={loading}
            rows={2}
            className="w-full bg-[#0f172a] text-[#e2e8f0] placeholder:text-[#64748b] border border-[#334155] rounded-xl px-4 py-3 text-sm resize-none focus:outline-none focus:border-[#06b6d4] transition-colors disabled:opacity-50"
          />
        </div>
        <button
          onClick={handleSubmit}
          disabled={!text.trim() || loading}
          className="shrink-0 flex items-center gap-2 px-4 py-3 bg-[#06b6d4] hover:bg-[#0891b2] text-white rounded-xl text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {loading ? (
            <>
              <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              查询中
            </>
          ) : (
            <>
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
              提问
            </>
          )}
        </button>
      </div>

      {/* Example Questions */}
      <div className="flex flex-wrap gap-2 mt-3">
        {examples.map((example) => (
          <button
            key={example}
            onClick={() => handleExampleClick(example)}
            disabled={loading}
            className="px-3 py-1 text-xs text-[#94a3b8] bg-[#0f172a] border border-[#334155] rounded-full hover:border-[#06b6d4] hover:text-[#06b6d4] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {example}
          </button>
        ))}
      </div>

      {/* Hint */}
      <div className="mt-2 text-xs text-[#64748b]">
        Enter 发送 · Shift+Enter 换行
      </div>
    </div>
  );
}

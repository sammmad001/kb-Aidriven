import { useState, useCallback } from 'react';
import QueryInput from '../components/qa/QueryInput';
import QueryResultDisplay from '../components/qa/QueryResultDisplay';
import QueryHistory from '../components/qa/QueryHistory';
import { useQueryHistory } from '../hooks/useQueryHistory';
import { api } from '../api/client';
import type { QueryResult } from '../api/types';

const EXAMPLE_QUESTIONS = [
  '什么是RAG？',
  '知识图谱和向量数据库有什么区别？',
  '为什么需要隐式关系推理？',
  'Agent架构的优缺点是什么？',
];

export default function QAPage() {
  const [result, setResult] = useState<QueryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentQuestion, setCurrentQuestion] = useState('');
  const { history, add, remove, clear } = useQueryHistory();

  const handleSubmit = useCallback(async (question: string) => {
    setLoading(true);
    setError(null);
    setCurrentQuestion(question);
    setResult(null);

    try {
      const res = await api.query(question);
      setResult(res);
      add({
        question,
        answer: res.answer,
        query_type: res.query_type,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : '查询失败');
    } finally {
      setLoading(false);
    }
  }, [add]);

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Main Q&A Area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Query Input */}
        <div className="p-4 border-b border-[#334155] bg-[#1e293b]">
          <QueryInput
            onSubmit={handleSubmit}
            loading={loading}
            placeholder="输入你的问题..."
            examples={EXAMPLE_QUESTIONS}
          />
        </div>

        {/* Result Display */}
        <div className="flex-1 overflow-y-auto p-6">
          {loading && (
            <div className="flex items-center justify-center h-40">
              <div className="flex flex-col items-center gap-3">
                <div className="w-8 h-8 border-2 border-[#06b6d4] border-t-transparent rounded-full animate-spin" />
                <span className="text-sm text-[#64748b]">正在查询知识库...</span>
              </div>
            </div>
          )}

          {error && !loading && (
            <div className="max-w-3xl mx-auto">
              <div className="text-sm text-[#ef4444] bg-[#ef444420] rounded-lg p-4">
                查询失败: {error}
              </div>
            </div>
          )}

          {result && !loading && !error && (
            <QueryResultDisplay
              result={result}
              question={currentQuestion}
            />
          )}

          {!result && !loading && !error && (
            <div className="max-w-3xl mx-auto text-center mt-16">
              <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-[#06b6d4] to-[#8b5cf6] flex items-center justify-center">
                <svg className="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                </svg>
              </div>
              <h2 className="text-lg font-semibold text-[#e2e8f0] mb-2">智能问答</h2>
              <p className="text-sm text-[#64748b]">
                基于知识图谱的智能问答系统，支持事实查询、关系推理、深度分析等多种查询类型
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Right Sidebar - Query History */}
      <aside className="w-[280px] border-l border-[#334155] bg-[#0f172a] shrink-0">
        <QueryHistory
          history={history}
          onSelect={(item) => handleSubmit(item.question)}
          onRemove={remove}
          onClear={clear}
        />
      </aside>
    </div>
  );
}

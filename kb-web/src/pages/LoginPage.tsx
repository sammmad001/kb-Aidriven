import { useState } from 'react';
import { useAuth } from '../auth/AuthContext';

export default function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === 'login') {
        await login(username, password);
      } else {
        await register(username, password);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Operation failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="w-screen h-screen flex items-center justify-center bg-[#0f172a]">
      <div className="w-[380px] bg-[#1e293b] rounded-2xl border border-[#334155] p-8 shadow-2xl">
        {/* Logo */}
        <div className="flex flex-col items-center mb-6">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-[#06b6d4] to-[#8b5cf6] flex items-center justify-center mb-3">
            <svg className="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <h1 className="text-xl font-bold text-[#e2e8f0]">个人知识库</h1>
          <p className="text-sm text-[#64748b] mt-1">
            {mode === 'login' ? '登录你的账户' : '创建新账户'}
          </p>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div>
            <label className="text-xs font-medium text-[#94a3b8] mb-1.5 block">用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              minLength={3}
              autoComplete="username"
              className="w-full px-3 py-2.5 bg-[#0f172a] border border-[#334155] rounded-lg text-[#e2e8f0] text-sm focus:border-[#06b6d4] focus:outline-none transition-colors"
              placeholder="3-50 个字符"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-[#94a3b8] mb-1.5 block">密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              className="w-full px-3 py-2.5 bg-[#0f172a] border border-[#334155] rounded-lg text-[#e2e8f0] text-sm focus:border-[#06b6d4] focus:outline-none transition-colors"
              placeholder="至少 6 位"
            />
          </div>

          {error && (
            <div className="px-3 py-2 bg-[#7f1d1d]/30 border border-[#991b1b] rounded-lg">
              <p className="text-sm text-[#fca5a5]">{error}</p>
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 bg-gradient-to-r from-[#06b6d4] to-[#0891b2] text-white rounded-lg text-sm font-medium hover:from-[#0891b2] hover:to-[#0e7490] transition-all disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? '请稍候...' : mode === 'login' ? '登 录' : '注 册'}
          </button>
        </form>

        {/* Switch mode */}
        <div className="text-center mt-5">
          <button
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login');
              setError(null);
            }}
            className="text-xs text-[#64748b] hover:text-[#06b6d4] transition-colors"
          >
            {mode === 'login' ? '没有账户？点击注册' : '已有账户？点击登录'}
          </button>
        </div>
      </div>
    </div>
  );
}

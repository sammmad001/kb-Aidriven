import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';

const ACCESS_KEY = 'kb_access_token';
const REFRESH_KEY = 'kb_refresh_token';
const USER_KEY = 'kb_username';

interface AuthState {
  token: string | null;
  username: string | null;
  isAuthenticated: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => void;
  getAccessToken: () => string | null;
  refreshAccessToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(ACCESS_KEY));
  const [username, setUsername] = useState<string | null>(() => localStorage.getItem(USER_KEY));

  const persist = (access: string, refresh: string, name: string) => {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
    localStorage.setItem(USER_KEY, name);
    setToken(access);
    setUsername(name);
  };

  const clear = () => {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
    setToken(null);
    setUsername(null);
  };

  const login = useCallback(async (user: string, pass: string) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: 'Login failed' }));
      throw new Error(detail.detail || 'Login failed');
    }
    const data = await res.json();
    persist(data.access_token, data.refresh_token, user);
  }, []);

  const register = useCallback(async (user: string, pass: string) => {
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: 'Registration failed' }));
      throw new Error(detail.detail || 'Registration failed');
    }
    const data = await res.json();
    persist(data.access_token, data.refresh_token, user);
  }, []);

  const logout = useCallback(() => clear(), []);

  const getAccessToken = useCallback(() => localStorage.getItem(ACCESS_KEY), []);

  const refreshAccessToken = useCallback(async (): Promise<string | null> => {
    const refresh = localStorage.getItem(REFRESH_KEY);
    if (!refresh) return null;
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!res.ok) {
        clear();
        return null;
      }
      const data = await res.json();
      localStorage.setItem(ACCESS_KEY, data.access_token);
      localStorage.setItem(REFRESH_KEY, data.refresh_token);
      setToken(data.access_token);
      return data.access_token;
    } catch {
      clear();
      return null;
    }
  }, []);

  // No mount effect needed — initial state already reads from localStorage

  return (
    <AuthContext.Provider
      value={{
        token,
        username,
        isAuthenticated: !!token,
        login,
        register,
        logout,
        getAccessToken,
        refreshAccessToken,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}

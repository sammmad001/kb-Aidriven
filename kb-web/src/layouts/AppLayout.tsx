import { NavLink, Outlet } from 'react-router-dom';

export default function AppLayout() {
  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
      isActive
        ? 'bg-[#06b6d4] text-white'
        : 'text-[#94a3b8] hover:text-[#e2e8f0] hover:bg-[#334155]'
    }`;

  return (
    <div className="w-screen h-screen flex flex-col bg-[#0f172a]">
      {/* Top Bar */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-[#334155] bg-[#1e293b] shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#06b6d4] to-[#8b5cf6] flex items-center justify-center">
            <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <nav className="flex items-center gap-1">
            <NavLink to="/" className={navLinkClass} end>
              图谱视图
            </NavLink>
            <NavLink to="/qa" className={navLinkClass}>
              智能问答
            </NavLink>
          </nav>
        </div>
      </header>

      {/* Page Content */}
      <Outlet />
    </div>
  );
}

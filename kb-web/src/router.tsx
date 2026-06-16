import { createBrowserRouter } from 'react-router-dom';
import AppLayout from './layouts/AppLayout';
import GraphPage from './pages/GraphPage';
import QAPage from './pages/QAPage';
import NodeDetailPage from './pages/NodeDetailPage';
import LoginPage from './pages/LoginPage';
import ProtectedRoute from './auth/ProtectedRoute';

export const router = createBrowserRouter(
  [
    {
      path: '/login',
      element: <LoginPage />,
    },
    {
      path: '/',
      element: (
        <ProtectedRoute>
          <AppLayout />
        </ProtectedRoute>
      ),
      children: [
        { index: true, element: <GraphPage /> },
        { path: 'qa', element: <QAPage /> },
        { path: 'node/:id', element: <NodeDetailPage /> },
      ],
    },
  ],
  { basename: '/ui' },
);

import { createBrowserRouter } from 'react-router-dom';
import AppLayout from './layouts/AppLayout';
import GraphPage from './pages/GraphPage';
import QAPage from './pages/QAPage';
import NodeDetailPage from './pages/NodeDetailPage';

export const router = createBrowserRouter(
  [
    {
      path: '/',
      element: <AppLayout />,
      children: [
        { index: true, element: <GraphPage /> },
        { path: 'qa', element: <QAPage /> },
        { path: 'node/:id', element: <NodeDetailPage /> },
      ],
    },
  ],
  { basename: '/ui' },
);

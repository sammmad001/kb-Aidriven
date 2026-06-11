import type { StylesheetJson } from 'cytoscape';

// Neon color palette for clusters
export const CLUSTER_COLORS = [
  '#06b6d4', // cyan
  '#8b5cf6', // purple
  '#f59e0b', // amber
  '#10b981', // green
  '#ef4444', // red
  '#ec4899', // pink
  '#3b82f6', // blue
  '#14b8a6', // teal
  '#f97316', // orange
  '#a855f7', // violet
];

// Implicit relation type colors
export const IMPLICIT_TYPE_COLORS: Record<string, string> = {
  depends_on: '#3b82f6',
  trade_off: '#ef4444',
  bridges: '#f59e0b',
  evolves_to: '#10b981',
  solves: '#a855f7',
};

export function getClusterColor(clusterId: number): string {
  return CLUSTER_COLORS[clusterId % CLUSTER_COLORS.length];
}

// Shape mapping by node type
function getShape(nodeType: string): string {
  switch (nodeType) {
    case 'Concept':
      return 'diamond';
    case 'Comparison':
      return 'hexagon';
    default:
      return 'ellipse';
  }
}

export function getStylesheet(): StylesheetJson {
  return [
    // Base node style
    {
      selector: 'node',
      style: {
        label: 'data(label)',
        'text-valign': 'center',
        'text-halign': 'center',
        'font-size': '11px',
        color: '#e2e8f0',
        'text-outline-color': '#0f172a',
        'text-outline-width': 2,
        'background-color': (ele: cytoscape.NodeSingular) => {
          const clusterId = ele.data('group') ?? 0;
          return getClusterColor(clusterId);
        },
        width: (ele: cytoscape.NodeSingular) => {
          const pr = ele.data('value') ?? 0.01;
          return Math.max(25, Math.min(65, 25 + pr * 120));
        },
        height: (ele: cytoscape.NodeSingular) => {
          const pr = ele.data('value') ?? 0.01;
          return Math.max(25, Math.min(65, 25 + pr * 120));
        },
        shape: (ele: cytoscape.NodeSingular) => {
          return getShape(ele.data('type') ?? 'Entity');
        },
        'border-width': 2,
        'border-color': (ele: cytoscape.NodeSingular) => {
          const clusterId = ele.data('group') ?? 0;
          return getClusterColor(clusterId);
        },
        'border-opacity': 0.6,
        opacity: 0.85,
        'shadow-blur': 15,
        'shadow-color': (ele: cytoscape.NodeSingular) => {
          const clusterId = ele.data('group') ?? 0;
          return getClusterColor(clusterId);
        },
        'shadow-opacity': 0.4,
        'z-index': 10,
      } as cytoscape.Css.Node,
    },
    // Selected node
    {
      selector: 'node:selected',
      style: {
        'border-width': 4,
        'border-color': '#fbbf24',
        'border-opacity': 1,
        opacity: 1,
        'shadow-blur': 25,
        'shadow-opacity': 0.8,
        'z-index': 999,
        width: (ele: cytoscape.NodeSingular) => {
          const pr = ele.data('value') ?? 0.01;
          return Math.max(30, Math.min(75, 30 + pr * 140));
        },
        height: (ele: cytoscape.NodeSingular) => {
          const pr = ele.data('value') ?? 0.01;
          return Math.max(30, Math.min(75, 30 + pr * 140));
        },
      } as cytoscape.Css.Node,
    },
    // Highlighted node (search result)
    {
      selector: 'node.highlighted',
      style: {
        'border-width': 4,
        'border-color': '#fbbf24',
        opacity: 1,
        'shadow-blur': 30,
        'shadow-opacity': 1,
        'z-index': 998,
      } as cytoscape.Css.Node,
    },
    // Dimmed node (when filtering)
    {
      selector: 'node.dimmed',
      style: {
        opacity: 0.15,
        'shadow-blur': 0,
      } as cytoscape.Css.Node,
    },
    // Explicit edge
    {
      selector: 'edge[?explicit]',
      style: {
        width: 2,
        'line-color': '#475569',
        'target-arrow-color': '#475569',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.8,
        'curve-style': 'bezier',
        opacity: 0.6,
      } as cytoscape.Css.Edge,
    },
    // Implicit edge
    {
      selector: 'edge[?implicit]',
      style: {
        width: (ele: cytoscape.EdgeSingular) => {
          const conf = ele.data('confidence') ?? 0.5;
          return 1 + conf * 3;
        },
        'line-color': (ele: cytoscape.EdgeSingular) => {
          const implicitType = ele.data('implicit_type');
          return implicitType ? (IMPLICIT_TYPE_COLORS[implicitType] ?? '#64748b') : '#64748b';
        },
        'target-arrow-color': (ele: cytoscape.EdgeSingular) => {
          const implicitType = ele.data('implicit_type');
          return implicitType ? (IMPLICIT_TYPE_COLORS[implicitType] ?? '#64748b') : '#64748b';
        },
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.7,
        'line-style': 'dashed',
        'line-dash-pattern': [6, 3],
        'curve-style': 'bezier',
        opacity: 0.7,
        label: 'data(edgeLabel)',
        'font-size': '9px',
        color: '#94a3b8',
        'text-outline-color': '#0f172a',
        'text-outline-width': 1,
        'text-rotation': 'autorotate',
      } as cytoscape.Css.Edge,
    },
    // Highlighted edge (connected to selected node)
    {
      selector: 'edge.edgeHighlighted',
      style: {
        opacity: 1,
        width: 3,
        'shadow-blur': 8,
        'shadow-color': '#fbbf24',
        'shadow-opacity': 0.5,
        'z-index': 999,
      } as cytoscape.Css.Edge,
    },
    // Dimmed edge
    {
      selector: 'edge.dimmed',
      style: {
        opacity: 0.05,
      } as cytoscape.Css.Edge,
    },
  ];
}

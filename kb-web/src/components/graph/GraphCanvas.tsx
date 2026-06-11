import { useEffect, useRef, useCallback } from 'react';
import cytoscape from 'cytoscape';
import fcose from 'cytoscape-fcose';
import type { GraphData } from '../../api/types';
import { getStylesheet } from './graphStyles';
import { startAnimations, stopAnimations, highlightConnectedEdges, resetEdgeHighlights } from './graphAnimations';

// Register fcose layout
cytoscape.use(fcose);

interface GraphCanvasProps {
  data: GraphData | null;
  onNodeClick: (nodeId: string) => void;
  onNodeDoubleClick: (nodeId: string) => void;
  highlightNodeId?: string | null;
}

export default function GraphCanvas({ data, onNodeClick, onNodeDoubleClick, highlightNodeId }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  // Initialize Cytoscape
  useEffect(() => {
    if (!containerRef.current) return;

    const cy = cytoscape({
      container: containerRef.current,
      style: getStylesheet(),
      wheelSensitivity: 0.15,
      minZoom: 0.2,
      maxZoom: 5,
      layout: { name: 'preset' },
    });

    cyRef.current = cy;

    // Tooltip handling
    const tooltip = tooltipRef.current;

    cy.on('mouseover', 'node', (evt) => {
      if (!tooltip) return;
      const node = evt.target;
      const pos = evt.renderedPosition;
      tooltip.innerHTML = `<strong>${node.data('label')}</strong><br/><span style="color:#94a3b8">${node.data('title') || ''}</span>`;
      tooltip.style.display = 'block';
      tooltip.style.left = `${pos.x + 15}px`;
      tooltip.style.top = `${pos.y - 10}px`;
    });

    cy.on('mouseout', 'node', () => {
      if (tooltip) tooltip.style.display = 'none';
    });

    cy.on('mouseover', 'edge', (evt) => {
      if (!tooltip) return;
      const edge = evt.target;
      const pos = evt.renderedPosition;
      const label = edge.data('edgeLabel') || edge.data('label') || '';
      const conf = edge.data('confidence');
      const confText = conf ? ` (${(conf * 100).toFixed(0)}%)` : '';
      tooltip.innerHTML = `<span style="color:#94a3b8">${label}${confText}</span>`;
      tooltip.style.display = 'block';
      tooltip.style.left = `${pos.x + 15}px`;
      tooltip.style.top = `${pos.y - 10}px`;
    });

    cy.on('mouseout', 'edge', () => {
      if (tooltip) tooltip.style.display = 'none';
    });

    // Node click → select + report
    cy.on('tap', 'node', (evt) => {
      const node = evt.target;
      const nodeId = node.data('id');
      onNodeClick(nodeId);
      highlightConnectedEdges(cy, nodeId);
    });

    // Double click → expand neighborhood
    cy.on('dbltap', 'node', (evt) => {
      const node = evt.target;
      onNodeDoubleClick(node.data('id'));
    });

    // Click on background → deselect
    cy.on('tap', (evt) => {
      if (evt.target === cy) {
        cy.nodes().unselect();
        resetEdgeHighlights(cy);
      }
    });

    return () => {
      stopAnimations();
      cy.destroy();
      cyRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Load data into Cytoscape
  const loadData = useCallback(
    (graphData: GraphData) => {
      const cy = cyRef.current;
      if (!cy) return;

      cy.elements().remove();

      const elements: cytoscape.ElementDefinition[] = [];

      // Add nodes
      for (const node of graphData.nodes) {
        elements.push({
          data: {
            id: node.id,
            label: node.label,
            title: node.title,
            group: node.group,
            value: node.value,
            type: node.type,
          },
        });
      }

      // Add edges
      graphData.edges.forEach((edge, idx) => {
        const isImplicit = edge.dashes;
        elements.push({
          data: {
            id: `e${idx}`,
            source: edge.from,
            target: edge.to,
            label: edge.label,
            edgeLabel: edge.label,
            explicit: !isImplicit,
            implicit: isImplicit,
            dashes: edge.dashes,
            implicit_type: edge.implicit_type,
            confidence: edge.label.match(/\((\d+)%\)/) ? parseInt(edge.label.match(/\((\d+)%\)/)![1]) / 100 : undefined,
          },
        });
      });

      cy.add(elements);

      // Run fcose layout
      cy.layout({
        name: 'fcose',
        quality: 'proof',
        randomize: true,
        animate: false,
        nodeDimensionsIncludeLabels: true,
        idealEdgeLength: 100,
        nodeSeparation: 60,
        piTol: 0.0000001,
      } as cytoscape.LayoutOptions).run();

      // Fit to view
      cy.fit(undefined, 40);

      // Start animations
      startAnimations(cy);
    },
    []
  );

  // Update when data changes
  useEffect(() => {
    if (data) {
      loadData(data);
    }
  }, [data, loadData]);

  // Highlight a specific node (from search)
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.nodes().removeClass('highlighted');

    if (highlightNodeId) {
      const node = cy.getElementById(highlightNodeId);
      if (node.length > 0) {
        node.addClass('highlighted');
        cy.animate({
          center: { eles: node },
          zoom: 2,
        }, { duration: 500 });
      }
    }
  }, [highlightNodeId]);

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="w-full h-full" style={{ background: '#0f172a' }} />
      <div ref={tooltipRef} className="cy-tooltip" style={{ display: 'none' }} />
    </div>
  );
}

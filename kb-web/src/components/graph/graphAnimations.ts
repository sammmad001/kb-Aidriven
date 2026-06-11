import type cytoscape from 'cytoscape';

let pulseInterval: ReturnType<typeof setInterval> | null = null;
let dashOffset = 0;
let dashInterval: ReturnType<typeof setInterval> | null = null;

/**
 * Start the breathing pulse animation on all nodes.
 * Nodes oscillate opacity between 0.65 and 0.95.
 * Higher PageRank nodes pulse more prominently.
 */
export function startPulseAnimation(cy: cytoscape.Core) {
  if (pulseInterval) return;

  let phase = 0;
  pulseInterval = setInterval(() => {
    phase += 0.05;
    const baseOpacity = 0.75 + 0.2 * Math.sin(phase);

    cy.nodes().forEach((node) => {
      if (node.selected() || node.hasClass('highlighted') || node.hasClass('dimmed')) return;
      const pr = node.data('value') ?? 0.01;
      // Higher PageRank = more dramatic pulse
      const amplitude = 0.1 + pr * 0.3;
      const nodeOpacity = baseOpacity + amplitude * Math.sin(phase + pr * 10);
      node.style('opacity', Math.max(0.5, Math.min(1, nodeOpacity)));
    });
  }, 50);
}

/**
 * Stop the pulse animation.
 */
export function stopPulseAnimation() {
  if (pulseInterval) {
    clearInterval(pulseInterval);
    pulseInterval = null;
  }
}

/**
 * Start the flowing dash animation on implicit edges.
 * Creates a signal-flow effect by shifting the dash offset.
 */
export function startDashFlowAnimation(cy: cytoscape.Core) {
  if (dashInterval) return;

  dashInterval = setInterval(() => {
    dashOffset = (dashOffset + 1) % 18; // 6+3+6+3 = 18 pattern length
    cy.edges('[?implicit]').forEach((edge) => {
      if (!edge.hasClass('dimmed')) {
        (edge.style() as Record<string, unknown>)['line-dash-offset'] = dashOffset;
        edge.style('line-dash-offset', dashOffset);
      }
    });
  }, 60);
}

/**
 * Stop the dash flow animation.
 */
export function stopDashFlowAnimation() {
  if (dashInterval) {
    clearInterval(dashInterval);
    dashInterval = null;
  }
}

/**
 * Start all animations.
 */
export function startAnimations(cy: cytoscape.Core) {
  startPulseAnimation(cy);
  startDashFlowAnimation(cy);
}

/**
 * Stop all animations.
 */
export function stopAnimations() {
  stopPulseAnimation();
  stopDashFlowAnimation();
}

/**
 * Highlight edges connected to a selected node.
 */
export function highlightConnectedEdges(cy: cytoscape.Core, nodeId: string) {
  // Reset all
  cy.edges().removeClass('edgeHighlighted dimmed');

  // Highlight connected edges
  const node = cy.getElementById(nodeId);
  if (node.length > 0) {
    const connectedEdges = node.connectedEdges();
    cy.edges().not(connectedEdges).addClass('dimmed');
    connectedEdges.addClass('edgeHighlighted');
  }
}

/**
 * Reset all edge highlights.
 */
export function resetEdgeHighlights(cy: cytoscape.Core) {
  cy.edges().removeClass('edgeHighlighted dimmed');
}

// API response types matching backend models

export interface GraphNode {
  id: string;
  label: string;
  title: string;
  group: number;
  value: number;
  type: 'Entity' | 'Concept' | 'Comparison';
}

export interface GraphEdge {
  from: string;
  to: string;
  label: string;
  dashes: boolean;
  implicit_type?: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface SearchResult {
  id: string;
  name: string;
  summary: string;
  type: 'Entity' | 'Concept' | 'Comparison';
  cluster_id: number;
  page_rank: number;
}

export interface RelationDetail {
  source_id: string;
  source_name: string;
  target_id: string;
  target_name: string;
  rel_type: string;
  implicit_type?: string;
  confidence?: number;
  evidence?: string;
  direction: 'outgoing' | 'incoming';
}

export interface NodeDetail {
  id: string;
  name: string;
  node_type: string;
  summary: string;
  content: string;
  tags: string[];
  page_rank: number;
  cluster_id: number;
  in_degree: number;
  out_degree: number;
  created_at?: string;
  updated_at?: string;
  relations: RelationDetail[];
}

export interface MultiHopPath {
  target_id: string;
  target_name: string;
  hop_count: number;
  path_nodes: string[];
  path_relations: string[];
}

export interface ClusterBrief {
  cluster_id: number;
  label: string;
  node_count: number;
  summary: string;
}

export interface KnowledgeChainReport {
  node: NodeDetail;
  direct_relations: RelationDetail[];
  multi_hop_paths: MultiHopPath[];
  implicit_relations: RelationDetail[];
  cluster_info?: ClusterBrief;
  metrics: Record<string, unknown>;
}

export interface GraphStats {
  node_count: number;
  edge_count: number;
  cluster_count: number;
  entity_count: number;
  concept_count: number;
  implicit_edge_count: number;
}

// =====================================================================
// Q&A Types (matching backend QueryResult model)
// =====================================================================

export interface QueryRequest {
  question: string;
}

export type QueryType = 'factual' | 'relational' | 'reasoning' | 'global';

export interface SourceReference {
  node_id: string;
  node_name: string;
  relevance: number;
}

export interface ImplicitRelationResult {
  source: string;
  target: string;
  type: string;
  confidence: number;
  evidence: string;
}

export interface QueryResult {
  answer: string;
  sources: SourceReference[];
  implicit_relations_used: ImplicitRelationResult[];
  confidence: number;
  query_type: QueryType;
  depth: number;
}

export interface QueryHistoryItem {
  question: string;
  answer: string;
  query_type: QueryType;
  timestamp: number;
}

"""API routes: graph data, statistics, lint, search, node detail, node report."""

from __future__ import annotations


import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import verify_api_token
from app.models import (
    GraphStats,
    KnowledgeChainReport,
    LintReport,
    NodeDetail,
    RelationDetail,
    MultiHopPath,
    ClusterBrief,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["graph"])

_db = None
_lint_checker = None


def set_db(db, lint_checker=None) -> None:
    global _db, _lint_checker
    _db = db
    _lint_checker = lint_checker


@router.get("/graph/stats", response_model=GraphStats, dependencies=[Depends(verify_api_token)])
async def get_graph_stats() -> GraphStats:
    """Get knowledge graph statistics."""
    if _db is None:
        return GraphStats()

    try:
        records = await _db.execute_read(
            """
            MATCH (n) WHERE n:Entity OR n:Concept
            WITH count(n) AS node_count
            OPTIONAL MATCH ()-[r]->()
            WITH node_count, count(r) AS edge_count
            OPTIONAL MATCH ()-[r:IMPLICIT]->()
            WITH node_count, edge_count, count(r) AS implicit_count
            OPTIONAL MATCH (e:Entity)
            WITH node_count, edge_count, implicit_count, count(e) AS entity_count
            OPTIONAL MATCH (c:Concept)
            WITH node_count, edge_count, implicit_count, entity_count, count(c) AS concept_count
            OPTIONAL MATCH (cl:Cluster)
            RETURN node_count, edge_count, implicit_count, entity_count, concept_count, count(cl) AS cluster_count
            """
        )
        if records:
            r = records[0]
            return GraphStats(
                node_count=r.get("node_count", 0),
                edge_count=r.get("edge_count", 0),
                implicit_edge_count=r.get("implicit_count", 0),
                entity_count=r.get("entity_count", 0),
                concept_count=r.get("concept_count", 0),
                cluster_count=r.get("cluster_count", 0),
            )
    except Exception as exc:
        logger.warning("Failed to fetch graph stats: %s", exc)

    return GraphStats()


@router.get("/graph/data", dependencies=[Depends(verify_api_token)])
async def get_graph_data(limit: int = Query(500, le=2000, description="Max nodes to return")) -> dict:
    """Get graph data in vis-network format for visualization."""
    if _db is None:
        return {"nodes": [], "edges": []}

    nodes = []
    edges = []

    # Fetch nodes (with type for shape mapping)
    node_records = await _db.execute_read(
        """
        MATCH (n) WHERE n:Entity OR n:Concept OR n:Comparison
        RETURN n.id AS id, n.name AS label, n.summary AS title,
               n.cluster_id AS `group`, n.page_rank AS value,
               CASE
                 WHEN n:Comparison THEN 'Comparison'
                 WHEN n:Concept THEN 'Concept'
                 ELSE 'Entity'
               END AS node_type
        ORDER BY n.page_rank DESC
        LIMIT $limit
        """,
        {"limit": limit},
    )
    for r in node_records:
        nodes.append({
            "id": r["id"],
            "label": r.get("label", r["id"]),
            "title": r.get("title", ""),
            "group": r.get("group", 0),
            "value": r.get("value", 0.01),
            "type": r.get("node_type", "Entity"),
        })

    # Fetch edges between returned nodes only (bounded)
    node_ids = [n["id"] for n in nodes]
    if node_ids:
        edge_records = await _db.execute_read(
            """
            MATCH (a)-[r]->(b)
            WHERE a.id IN $node_ids AND b.id IN $node_ids
            RETURN a.id AS from, b.id AS to,
                   CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS label,
                   r.confidence AS confidence,
                   CASE WHEN type(r) = 'IMPLICIT' THEN true ELSE false END AS dashes,
                   r.implicit_type AS implicit_type
            """,
            {"node_ids": node_ids},
        )
        for r in edge_records:
            edge = {
                "from": r["from"],
                "to": r["to"],
                "label": r.get("label", ""),
                "dashes": r.get("dashes", False),
            }
            if r.get("implicit_type"):
                edge["implicit_type"] = r["implicit_type"]
            if r.get("confidence"):
                edge["label"] += f" ({r['confidence']:.0%})"
            edges.append(edge)

    return {"nodes": nodes, "edges": edges}


@router.get("/graph/neighbors/{entity_id}", dependencies=[Depends(verify_api_token)])
async def get_neighbors(entity_id: str, depth: int = 1) -> dict:
    """Get N-hop neighborhood of an entity."""
    if _db is None:
        return {"nodes": [], "edges": []}

    nodes = []
    edges = []

    # Get the target node
    target = await _db.get_node_by_id(entity_id)
    if target:
        nodes.append({"id": entity_id, "label": target.get("name", entity_id)})

    # Get neighbors
    records = await _db.execute_read(
        """
        MATCH (a)-[r*1..2]-(b)
        WHERE a.id = $id
        RETURN DISTINCT b.id AS id, b.name AS name, b.summary AS summary,
               labels(b) AS labels
        LIMIT 20
        """,
        {"id": entity_id},
    )
    for r in records:
        nodes.append({"id": r["id"], "label": r.get("name", ""), "summary": r.get("summary", "")})

    # Get edges between these nodes
    node_ids = [n["id"] for n in nodes]
    if node_ids:
        edge_records = await _db.execute_read(
            """
            MATCH (a)-[r]->(b)
            WHERE a.id IN $ids AND b.id IN $ids
            RETURN a.id AS from, b.id AS to,
                   CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS label
            """,
            {"ids": node_ids},
        )
        for r in edge_records:
            edges.append({"from": r["from"], "to": r["to"], "label": r.get("label", "")})

    return {"nodes": nodes, "edges": edges}


@router.get("/graph/path", dependencies=[Depends(verify_api_token)])
async def get_path(from_id: str, to_id: str) -> dict:
    """Find shortest path between two entities."""
    if _db is None:
        return {"path": []}

    records = await _db.execute_read(
        """
        MATCH path = shortestPath((a)-[*..6]-(b))
        WHERE a.id = $from_id AND b.id = $to_id
        RETURN [node in nodes(path) | {id: node.id, name: node.name}] AS nodes,
               [rel in relationships(path) | {
                   from: startNode(rel).name,
                   to: endNode(rel).name,
                   type: CASE WHEN rel.type IS NOT NULL THEN rel.type ELSE type(rel) END
               }] AS edges
        LIMIT 1
        """,
        {"from_id": from_id, "to_id": to_id},
    )

    if records:
        return {"nodes": records[0]["nodes"], "edges": records[0]["edges"]}
    return {"nodes": [], "edges": []}


@router.post("/lint", response_model=LintReport, dependencies=[Depends(verify_api_token)])
async def run_lint() -> LintReport:
    """Execute quality check on the knowledge graph."""
    if _lint_checker is None:
        return LintReport()
    return await _lint_checker.run_all_checks()


# ---------------------------------------------------------------------------
# New endpoints: node detail, search, knowledge chain report
# ---------------------------------------------------------------------------


@router.get("/graph/search", dependencies=[Depends(verify_api_token)])
async def search_entities(q: str = Query(..., min_length=1)) -> dict:
    """Search entities by keyword (name or summary)."""
    if _db is None:
        return {"results": []}

    records = await _db.execute_read(
        """
        MATCH (n)
        WHERE (n:Entity OR n:Concept OR n:Comparison)
          AND (n.name CONTAINS $kw OR n.summary CONTAINS $kw)
        RETURN n.id AS id, n.name AS name, n.summary AS summary,
               n.cluster_id AS cluster_id, n.page_rank AS page_rank,
               CASE
                 WHEN n:Comparison THEN 'Comparison'
                 WHEN n:Concept THEN 'Concept'
                 ELSE 'Entity'
               END AS node_type
        ORDER BY n.page_rank DESC
        LIMIT 20
        """,
        {"kw": q},
    )
    results = []
    for r in records:
        results.append({
            "id": r["id"],
            "name": r.get("name", ""),
            "summary": r.get("summary", ""),
            "type": r.get("node_type", "Entity"),
            "cluster_id": r.get("cluster_id", 0),
            "page_rank": r.get("page_rank", 0.0),
        })
    return {"results": results}


def _to_iso(dt) -> str | None:
    """Convert Neo4j DateTime or Python datetime to ISO string."""
    if dt is None:
        return None
    if hasattr(dt, 'iso_format'):  # neo4j.time.DateTime
        return dt.iso_format()
    if hasattr(dt, 'isoformat'):   # Python datetime
        return dt.isoformat()
    return str(dt)


@router.get("/graph/node/{node_id}", response_model=NodeDetail, dependencies=[Depends(verify_api_token)])
async def get_node_detail(node_id: str) -> NodeDetail:
    """Get full detail of a single node."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    # Get node data
    records = await _db.execute_read(
        """
        MATCH (n) WHERE n.id = $id
        RETURN n.id AS id, n.name AS name, n.summary AS summary,
               n.content AS content, n.tags AS tags,
               n.page_rank AS page_rank, n.cluster_id AS cluster_id,
               n.created_at AS created_at, n.updated_at AS updated_at,
               CASE
                 WHEN n:Comparison THEN 'Comparison'
                 WHEN n:Concept THEN 'Concept'
                 ELSE 'Entity'
               END AS node_type
        """,
        {"id": node_id},
    )
    if not records:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")

    r = records[0]
    # Get degree counts
    degree_records = await _db.execute_read(
        """
        MATCH (n) WHERE n.id = $id
        OPTIONAL MATCH (n)-[r_out]->() WITH count(r_out) AS out_deg
        OPTIONAL MATCH ()-[r_in]->(n) WITH out_deg, count(r_in) AS in_deg
        RETURN out_deg, in_deg
        """,
        {"id": node_id},
    )
    in_degree = 0
    out_degree = 0
    if degree_records:
        in_degree = degree_records[0].get("in_deg", 0)
        out_degree = degree_records[0].get("out_deg", 0)

    # Get relations
    rel_records = await _db.execute_read(
        """
        MATCH (a)-[r]->(b) WHERE a.id = $id
        RETURN a.id AS source_id, a.name AS source_name,
               b.id AS target_id, b.name AS target_name,
               CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS rel_type,
               r.implicit_type AS implicit_type,
               r.confidence AS confidence, r.evidence AS evidence,
               'outgoing' AS direction
        UNION
        MATCH (a)-[r]->(b) WHERE b.id = $id
        RETURN a.id AS source_id, a.name AS source_name,
               b.id AS target_id, b.name AS target_name,
               CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS rel_type,
               r.implicit_type AS implicit_type,
               r.confidence AS confidence, r.evidence AS evidence,
               'incoming' AS direction
        """,
        {"id": node_id},
    )
    relations = []
    for rec in rel_records:
        relations.append(RelationDetail(
            source_id=rec.get("source_id", ""),
            source_name=rec.get("source_name", ""),
            target_id=rec.get("target_id", ""),
            target_name=rec.get("target_name", ""),
            rel_type=rec.get("rel_type", ""),
            implicit_type=rec.get("implicit_type"),
            confidence=rec.get("confidence"),
            evidence=rec.get("evidence"),
            direction=rec.get("direction", "outgoing"),
        ))

    return NodeDetail(
        id=r["id"],
        name=r.get("name", ""),
        node_type=r.get("node_type", "Entity"),
        summary=r.get("summary", ""),
        content=r.get("content", ""),
        tags=r.get("tags") or [],
        page_rank=r.get("page_rank", 0.0),
        cluster_id=r.get("cluster_id") or 0,
        in_degree=in_degree,
        out_degree=out_degree,
        created_at=_to_iso(r.get("created_at")),
        updated_at=_to_iso(r.get("updated_at")),
        relations=relations,
    )


@router.get("/graph/node-report/{node_id}", response_model=KnowledgeChainReport, dependencies=[Depends(verify_api_token)])
async def get_node_report(node_id: str) -> KnowledgeChainReport:
    """Generate a complete knowledge chain report for a node."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    # 1. Get node detail
    node_detail = await get_node_detail(node_id)

    # 2. Separate explicit and implicit relations
    explicit_rels = [r for r in node_detail.relations if r.rel_type != "IMPLICIT"]
    implicit_rels = [r for r in node_detail.relations if r.rel_type == "IMPLICIT"]

    # 3. Multi-hop paths (2-3 hops)
    multi_hop_records = await _db.execute_read(
        """
        MATCH path = (a)-[*2..3]-(b)
        WHERE a.id = $id AND (b:Entity OR b:Concept OR b:Comparison)
        RETURN b.id AS target_id, b.name AS target_name,
               length(path) AS hop_count,
               [n IN nodes(path) | n.name] AS path_nodes,
               [r IN relationships(path) |
                 CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END
               ] AS path_relations
        ORDER BY hop_count ASC
        LIMIT 30
        """,
        {"id": node_id},
    )
    multi_hop_paths = []
    for rec in multi_hop_records:
        multi_hop_paths.append(MultiHopPath(
            target_id=rec.get("target_id", ""),
            target_name=rec.get("target_name", ""),
            hop_count=rec.get("hop_count", 0),
            path_nodes=rec.get("path_nodes", []),
            path_relations=rec.get("path_relations", []),
        ))

    # 4. Cluster info
    cluster_info = None
    if node_detail.cluster_id:
        cluster_records = await _db.execute_read(
            """
            MATCH (n) WHERE n.cluster_id = $cid AND (n:Entity OR n:Concept OR n:Comparison)
            WITH count(n) AS node_count
            OPTIONAL MATCH (c:Cluster) WHERE c.id = $cid
            RETURN $cid AS cluster_id, c.label AS label, node_count, c.summary AS summary
            """,
            {"cid": node_detail.cluster_id},
        )
        if cluster_records:
            cr = cluster_records[0]
            cluster_info = ClusterBrief(
                cluster_id=cr.get("cluster_id", node_detail.cluster_id),
                label=cr.get("label", "") or f"Cluster #{node_detail.cluster_id}",
                node_count=cr.get("node_count", 0),
                summary=cr.get("summary", ""),
            )

    # 5. Metrics
    total_nodes_records = await _db.execute_read(
        "MATCH (n) WHERE n:Entity OR n:Concept OR n:Comparison RETURN count(n) AS total"
    )
    total_nodes = total_nodes_records[0]["total"] if total_nodes_records else 1
    rank_percent = round((1 - node_detail.page_rank) * 100, 1) if node_detail.page_rank else 0

    # Bridge clusters
    bridge_records = await _db.execute_read(
        """
        MATCH (n)-[r]-(m)
        WHERE n.id = $id AND (m:Entity OR m:Concept OR m:Comparison)
        RETURN DISTINCT m.cluster_id AS cid
        """,
        {"id": node_id},
    )
    connected_clusters = set()
    for rec in bridge_records:
        cid = rec.get("cid")
        if cid is not None:
            connected_clusters.add(cid)

    metrics = {
        "page_rank": node_detail.page_rank,
        "rank_percent": f"Top {rank_percent}%",
        "in_degree": node_detail.in_degree,
        "out_degree": node_detail.out_degree,
        "total_nodes": total_nodes,
        "connected_clusters": len(connected_clusters),
        "is_bridge": len(connected_clusters) > 1,
    }

    return KnowledgeChainReport(
        node=node_detail,
        direct_relations=explicit_rels,
        multi_hop_paths=multi_hop_paths,
        implicit_relations=implicit_rels,
        cluster_info=cluster_info,
        metrics=metrics,
    )

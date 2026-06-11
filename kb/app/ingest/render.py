"""Step 4: Render Neo4j nodes and relationships into Markdown files."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from app.config import Settings
from app.database import Neo4jDatabase

logger = logging.getLogger(__name__)


class MarkdownRenderer:
    """Render Neo4j graph data into read-only Markdown files."""

    def __init__(self, db: Neo4jDatabase, settings: Settings) -> None:
        self._db = db
        self._wiki_dir = settings.wiki_dir
        os.makedirs(self._wiki_dir, exist_ok=True)

    async def render_affected(self, node_ids: list[str]) -> list[str]:
        """Render all affected nodes and special index files — parallelized."""
        # Parallelize all node renders + index + log
        node_tasks = [self.render_node(nid) for nid in node_ids]
        index_task = self.render_index()
        log_task = self.render_log()

        all_results = await asyncio.gather(
            *node_tasks, index_task, log_task,
            return_exceptions=True,
        )

        rendered: list[str] = []
        for r in all_results:
            if isinstance(r, str):
                rendered.append(r)
            elif isinstance(r, Exception):
                logger.warning("Render task failed: %s", r)
        return rendered

    async def render_node(self, node_id: str) -> str | None:
        """Render a single Neo4j node into a Markdown file."""
        # 1. Read node
        node = await self._db.get_node_by_id(node_id)
        if not node:
            logger.warning("Node not found for rendering: %s", node_id)
            return None

        # 2. Read relationships
        explicit_rels = await self._db.execute_read(
            """
            MATCH (n)-[r:EXPLICIT]->(m) WHERE n.id = $id
            RETURN r.type AS rel_type, m.name AS target_name, r.context AS context
            ORDER BY r.type
            """,
            {"id": node_id},
        )

        implicit_rels = await self._db.execute_read(
            """
            MATCH (n)-[r:IMPLICIT]->(m) WHERE n.id = $id
            RETURN r.type AS rel_type, m.name AS target_name,
                   r.confidence AS confidence, r.evidence AS evidence
            ORDER BY r.confidence DESC
            """,
            {"id": node_id},
        )

        # Also get incoming relationships
        incoming_rels = await self._db.execute_read(
            """
            MATCH (m)-[r]->(n) WHERE n.id = $id AND m <> n
            RETURN CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS rel_type,
                   m.name AS source_name, type(r) AS edge_label
            ORDER BY rel_type
            """,
            {"id": node_id},
        )

        # 3. Assemble Markdown
        md = f"# {node.get('name', node_id)}\n\n"

        summary = node.get("summary", "")
        if summary:
            md += f"> {summary}\n\n"

        content = node.get("content", "")
        if content:
            md += content + "\n"

        # Explicit relationships (outgoing)
        if explicit_rels:
            md += "\n## Relationships\n"
            for rel in explicit_rels:
                md += f"- **{rel['rel_type']}** → [[{rel['target_name']}]]"
                if rel.get("context"):
                    md += f" — _{rel['context']}_"
                md += "\n"

        # Incoming relationships
        if incoming_rels:
            md += "\n## Referenced by\n"
            seen = set()
            for rel in incoming_rels:
                key = f"{rel['source_name']}|{rel['rel_type']}"
                if key not in seen:
                    md += f"- ← **{rel['rel_type']}** from [[{rel['source_name']}]]\n"
                    seen.add(key)

        # Implicit relationships
        if implicit_rels:
            md += "\n## Implicit Relations\n"
            for rel in implicit_rels:
                conf = rel.get("confidence", 0)
                md += f"- *{rel['rel_type']}* → [[{rel['target_name']}]] ({conf:.0%})"
                if rel.get("evidence"):
                    md += f" — {rel['evidence']}"
                md += "\n"

        # Tags
        tags = node.get("tags", [])
        if tags:
            md += f"\nTags: {', '.join(tags)}\n"

        # Source
        source = node.get("source", "")
        if source:
            md += f"\nSource: `{source}`\n"

        # 4. Write file
        label = self._determine_label(node)
        subdir = os.path.join(self._wiki_dir, label)
        os.makedirs(subdir, exist_ok=True)

        filename = self._safe_filename(node.get("name", node_id)) + ".md"
        filepath = os.path.join(subdir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md)

        return filepath

    async def render_index(self) -> str:
        """Render the index.md file listing all nodes."""
        records = await self._db.execute_read(
            "MATCH (n) WHERE n:Entity OR n:Concept OR n:Comparison "
            "RETURN n.name AS name, n.summary AS summary, labels(n) AS labels, n.updated_at AS updated "
            "ORDER BY n.updated_at DESC"
        )

        md = "# Knowledge Base Index\n\n"
        md += f"_Auto-generated at {datetime.now().isoformat()}_\n\n"

        for r in records:
            labels = r.get("labels", [])
            label_str = labels[0] if labels else "Node"
            md += f"- **[{r['name']}]** ({label_str})"
            if r.get("summary"):
                md += f" — {r['summary']}"
            md += "\n"

        filepath = os.path.join(self._wiki_dir, "index.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md)
        return filepath

    async def render_log(self) -> str:
        """Render the log.md file with recent activity."""
        records = await self._db.execute_read(
            "MATCH (n) WHERE n:Entity OR n:Concept "
            "RETURN n.name AS name, n.created_at AS created, n.updated_at AS updated "
            "ORDER BY n.updated_at DESC LIMIT 30"
        )

        md = "# Activity Log\n\n"
        for r in records:
            updated = r.get("updated", "unknown")
            if hasattr(updated, "isoformat"):
                updated = updated.isoformat()
            md += f"- **{r['name']}** — updated {updated}\n"

        filepath = os.path.join(self._wiki_dir, "log.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md)
        return filepath

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _determine_label(node: dict) -> str:
        """Determine the subdirectory label from node data."""
        # This is a simplified approach - in practice labels come from Neo4j
        name = node.get("name", "")
        if " vs " in name.lower() or " vs. " in name.lower():
            return "comparisons"
        return "entities"

    @staticmethod
    def _safe_filename(name: str) -> str:
        """Convert node name to safe filename."""
        import re
        name = re.sub(r'[<>:"/\\|?*]', "", name)
        return name.strip() or "untitled"

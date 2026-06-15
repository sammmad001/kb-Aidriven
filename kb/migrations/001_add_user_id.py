"""Migration 001: Add user_id property to existing nodes.

This migration:
1. Sets user_id = "default" on all nodes that don't have one
2. Renames node IDs to namespaced format "{user_id}:{original_id}" for Entity/Concept
3. Updates Source node paths to include user_id directory

Idempotent: checks if migration has already been applied.
"""

from __future__ import annotations

import logging

from app.database import Neo4jDatabase

logger = logging.getLogger(__name__)

MIGRATION_ID = "001_add_user_id"


async def check_migrated(db: Neo4jDatabase) -> bool:
    """Check if this migration has already been applied."""
    records = await db.execute_read(
        "MATCH (n) WHERE n.user_id IS NULL AND (n:Entity OR n:Concept OR n:Source) "
        "RETURN count(n) AS unmarked_count LIMIT 1"
    )
    if records and records[0]["unmarked_count"] > 0:
        return False
    # Also check if any node already has a namespaced ID (indicates prior migration)
    namespaced = await db.execute_read(
        "MATCH (n) WHERE n.id CONTAINS ':' AND (n:Entity OR n:Concept) "
        "RETURN count(n) AS ns_count LIMIT 1"
    )
    return bool(namespaced and namespaced[0]["ns_count"] > 0)


async def run(db: Neo4jDatabase, default_user_id: str = "default") -> None:
    """Execute the migration."""
    already_done = await check_migrated(db)
    if already_done:
        logger.info("Migration %s already applied — skipping", MIGRATION_ID)
        return

    logger.info("Starting migration %s with default_user_id=%s", MIGRATION_ID, default_user_id)

    # Step 1: Set user_id on all nodes that don't have one
    result = await db.execute_write(
        "MATCH (n) WHERE n.user_id IS NULL "
        "SET n.user_id = $uid "
        "RETURN count(n) AS updated",
        {"uid": default_user_id},
    )
    updated_count = result[0]["updated"] if result else 0
    logger.info("Set user_id=%s on %d nodes", default_user_id, updated_count)

    # Step 2: Rename node IDs to namespaced format for Entity and Concept
    # Only rename IDs that don't already have a colon (avoid double-namespacing)
    rename_result = await db.execute_write(
        """
        MATCH (n)
        WHERE (n:Entity OR n:Concept)
          AND n.id IS NOT NULL
          AND NOT n.id CONTAINS ':'
          AND n.user_id = $uid
        WITH n, n.id AS old_id
        SET n.id = $uid + ':' + old_id
        RETURN count(n) AS renamed
        """,
        {"uid": default_user_id},
    )
    renamed_count = rename_result[0]["renamed"] if rename_result else 0
    logger.info("Namespaced %d node IDs with prefix '%s:'", renamed_count, default_user_id)

    # Step 3: Update Source node paths to include user_id subdirectory
    # raw/sources/{filename} → raw/sources/{user_id}/{filename}
    path_result = await db.execute_write(
        """
        MATCH (s:Source)
        WHERE s.raw_path IS NOT NULL
          AND s.user_id = $uid
          AND NOT s.raw_path CONTAINS ('/' + $uid + '/')
        WITH s, s.raw_path AS old_path
        SET s.raw_path = replace(old_path, 'raw/sources/', 'raw/sources/' + $uid + '/')
        RETURN count(s) AS paths_updated
        """,
        {"uid": default_user_id},
    )
    paths_updated = path_result[0]["paths_updated"] if path_result else 0
    logger.info("Updated %d Source paths to include user_id directory", paths_updated)

    logger.info("Migration %s completed successfully", MIGRATION_ID)

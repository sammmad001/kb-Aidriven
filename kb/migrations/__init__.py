"""Migration package."""

from importlib import import_module


async def run_all(db, default_user_id: str = "default") -> None:
    """Run all pending migrations in order."""
    # Import via import_module since module name starts with a digit
    mig_001 = import_module("migrations.001_add_user_id")
    await mig_001.run(db, default_user_id=default_user_id)


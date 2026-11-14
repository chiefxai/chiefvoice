from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

CURRENT_SCHEMA_REVISION = "0012_phone_number_ownership"
CONSTRAINT_NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


async def verify_schema_revision(engine: AsyncEngine) -> None:
    """Fail startup clearly when migrations have not been applied or are outdated."""
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            revisions = result.scalars().all()
    except SQLAlchemyError as exc:
        raise RuntimeError(
            "Database schema is not initialized; run `uv run alembic upgrade head`"
        ) from exc

    if revisions != [CURRENT_SCHEMA_REVISION]:
        revision_text = ", ".join(revisions) if revisions else "none"
        raise RuntimeError(
            "Database schema revision is not current "
            f"(found: {revision_text}); run `uv run alembic upgrade head`"
        )

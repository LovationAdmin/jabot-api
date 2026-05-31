from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def write_audit(
    db: AsyncSession,
    *,
    actor_user_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict | None = None,
):
    """Write an audit log entry and commit it.

    Never let an audit failure break the main operation: any error is
    swallowed (the session is rolled back so the caller can keep using it).
    """
    try:
        await db.execute(
            AuditLog.__table__.insert().values(
                actor_user_id=actor_user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details,
            )
        )
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

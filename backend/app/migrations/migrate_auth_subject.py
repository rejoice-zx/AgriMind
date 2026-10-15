"""Idempotent SQLite account-subject upgrade; called inside init_db's transaction.

No configuration or database is opened here. The caller supplies the existing
SQLAlchemy connection. Existing accounts get a new lifetime UUID and old JWTs
without the matching account_subject claim are deliberately invalidated.
"""
import uuid
from sqlalchemy import inspect, text


def migrate_auth_subjects(connection):
    inspector = inspect(connection)
    if not inspector.has_table("users"):
        raise RuntimeError("Create the users table before migrating auth subjects")
    if "auth_subject" not in {column["name"] for column in inspector.get_columns("users")}:
        connection.execute(text("ALTER TABLE users ADD COLUMN auth_subject VARCHAR(36)"))
    missing_ids = connection.execute(text(
        "SELECT id FROM users WHERE auth_subject IS NULL OR auth_subject = ''"
    )).scalars().all()
    for user_id in missing_ids:
        connection.execute(text("UPDATE users SET auth_subject=:subject WHERE id=:id"),
                           {"subject": str(uuid.uuid4()), "id": user_id})
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_auth_subject ON users(auth_subject)"
    ))
    # SQLite ADD COLUMN cannot add NOT NULL without a shared default. Enforce the
    # same invariant on upgraded tables without rebuilding users/its foreign keys.
    for operation in ("INSERT", "UPDATE"):
        connection.execute(text(f"""
            CREATE TRIGGER IF NOT EXISTS users_auth_subject_required_{operation.lower()}
            BEFORE {operation} ON users
            WHEN NEW.auth_subject IS NULL OR NEW.auth_subject = ''
            BEGIN SELECT RAISE(ABORT, 'account subject is required'); END
        """))

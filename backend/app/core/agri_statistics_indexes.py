"""Idempotent additive SQLite indexes for both new and previously created tables."""
from sqlalchemy import text

STATISTICS_INDEXES = (
    ("ix_agri_conversations_created", "agri_conversations", "created_at"),
    ("ix_agri_turns_created_status", "agri_turns", "created_at, status"),
    ("ix_agri_consultations_created_status", "agri_consultations", "created_at, status"),
    ("ix_agri_consultations_updated_status", "agri_consultations", "updated_at, status"),
    ("ix_farming_records_date", "farming_records", "record_date"),
)


async def ensure_agri_statistics_indexes(connection):
    for name, table, columns in STATISTICS_INDEXES:
        # Identifiers come exclusively from the constant tuples above, never request input.
        await connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))


def migrate_statistics_indexes(connection):
    for name, table, columns in STATISTICS_INDEXES:
        connection.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})')

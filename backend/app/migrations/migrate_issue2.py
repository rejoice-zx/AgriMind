"""Additive upgrade; unknown historical creators stay unknown."""
from sqlalchemy import inspect, text


def migrate_issue2(connection):
    additions = {
        'farming_records': {'created_by_subject': 'VARCHAR(64)', 'revoked_at': 'DATETIME',
            'revoked_by_subject': 'VARCHAR(64)', 'revoke_reason': 'VARCHAR(500)'},
        'farms': {'region_code': 'VARCHAR(12)', 'location_source': 'VARCHAR(30)', 'location_label': 'VARCHAR(255)'},
    }
    inspector = inspect(connection)
    for table, columns in additions.items():
        if not inspector.has_table(table): continue
        existing = {c['name'] for c in inspector.get_columns(table)}
        for column, sql_type in columns.items():
            if column not in existing:
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {sql_type}'))

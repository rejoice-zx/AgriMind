"""Add contact/review fields without inventing phones or pending states for legacy users."""
from sqlalchemy import inspect, text


def migrate_registration_review(connection):
    inspector = inspect(connection)
    if not inspector.has_table('users'):
        return
    existing = {column['name'] for column in inspector.get_columns('users')}
    columns = {'phone': 'VARCHAR(16)', 'registration_status': 'VARCHAR(20)',
               'registration_reviewed_at': 'DATETIME', 'registration_reviewed_by_subject': 'VARCHAR(64)'}
    for column, sql_type in columns.items():
        if column not in existing:
            connection.execute(text(f'ALTER TABLE users ADD COLUMN {column} {sql_type}'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_registration_status ON users (registration_status)'))

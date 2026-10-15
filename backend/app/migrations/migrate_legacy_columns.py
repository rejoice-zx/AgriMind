"""Preserve the former startup column/index upgrades as one explicit additive step."""
from sqlalchemy import inspect


def migrate_legacy_columns(connection):
    additions = {
        'farms': {'archived_at': 'DATETIME'},
        'fields': {'archived_at': 'DATETIME'},
        'conversations': {'customer_id': "VARCHAR(64) NOT NULL DEFAULT 'anonymous'",
            'temp_session_id': 'VARCHAR(128)', 'temp_device_id': 'VARCHAR(128)',
            'is_deleted_by_user': 'BOOLEAN NOT NULL DEFAULT 0',
            'deleted_by_customer_id': 'VARCHAR(64)', 'deleted_at': 'DATETIME'},
        'messages': {'human_agent_name': 'VARCHAR(100)', 'is_deleted_by_user': 'BOOLEAN NOT NULL DEFAULT 0',
            'deleted_by_customer_id': 'VARCHAR(64)', 'deleted_at': 'DATETIME'},
        'farming_records': {'safety_interval_days': 'INTEGER', 'request_id': 'VARCHAR(36)', 'request_fingerprint': 'VARCHAR(64)'},
        'knowledge_items': {'publish_date': 'VARCHAR(20)', 'url': 'VARCHAR(500)', 'source_updated_at': 'VARCHAR(20)'},
    }
    inspector = inspect(connection)
    for table, columns in additions.items():
        existing = {column['name'] for column in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in existing:
                connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} {sql_type}')
    indexes = {
        'conversations': {'customer_id': 'customer_id', 'temp_session_id': 'temp_session_id', 'temp_device_id': 'temp_device_id',
            'is_deleted_by_user': 'is_deleted_by_user', 'status_updated_at': 'status, updated_at', 'created_at': 'created_at'},
        'messages': {'is_deleted_by_user': 'is_deleted_by_user', 'deleted_by_customer_id': 'deleted_by_customer_id',
            'timestamp': 'timestamp', 'role_timestamp': 'role, timestamp',
            'conversation_deleted_timestamp': 'conversation_id, is_deleted_by_user, timestamp'},
    }
    for table, definitions in indexes.items():
        for name, columns in definitions.items():
            connection.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS ix_{table}_{name} ON {table} ({columns})')
    connection.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_farming_records_request_id ON farming_records(request_id)')

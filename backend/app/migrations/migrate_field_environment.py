"""Revision 3 additive field context; legacy names/remarks never imply a facility type."""
from sqlalchemy import inspect, text


def migrate_field_environment(connection):
    if 'facility_type' not in {column['name'] for column in inspect(connection).get_columns('fields')}:
        connection.execute(text("ALTER TABLE fields ADD COLUMN facility_type VARCHAR(20) NOT NULL DEFAULT 'unknown'"))

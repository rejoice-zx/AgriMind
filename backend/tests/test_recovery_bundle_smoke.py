"""Small recovery smoke check. Run directly; no deployment configuration or async DB."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.recovery_bundle import create_bundle, prepare_restore, snapshot_database
from app.core.schema_revision import guard_schema_revision, record_schema_revision, install_history_guards, CURRENT_SCHEMA_REVISION


class RecoverySmoke(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='agri-recovery-smoke-', dir=os.environ.get('AGRI_CHECK_SCRATCH'))
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def bundle(self):
        content = b'synthetic-image-content'
        name = 'a' * 32 + '.png'
        data = self.root / 'data'
        media = data / 'private_uploads' / 'diagnosis' / name
        media.parent.mkdir(parents=True); media.write_bytes(content)
        source, snapshot, bundle = self.root / 'live.db', self.root / 'snapshot.db', self.root / 'backup.zip'
        with closing(sqlite3.connect(source)) as live:
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('CREATE TABLE diagnosis_media(id TEXT PRIMARY KEY,sha256 TEXT,size_bytes INTEGER)')
            live.execute('INSERT INTO diagnosis_media VALUES(?,?,?)', (name, hashlib.sha256(content).hexdigest(), len(content)))
            live.commit()
            snapshot_database(source, snapshot)  # Writer remains open with committed WAL pages.
        create_bundle(snapshot, data, bundle, app_version='synthetic')
        return bundle, content, name

    def test_database_media_round_trip_and_existing_target_protected(self):
        bundle, content, name = self.bundle()
        target = self.root / 'restored'
        manifest = prepare_restore(bundle, target)
        self.assertEqual((target / 'private_uploads' / 'diagnosis' / name).read_bytes(), content)
        self.assertEqual(manifest['table_counts']['diagnosis_media'], 1)
        self.assertTrue(json.loads((target / 'READY.json').read_text(encoding='utf-8'))['verified'])
        with self.assertRaisesRegex(ValueError, '禁止覆盖'):
            prepare_restore(bundle, target)

    def test_tampered_media_cannot_receive_ready_marker(self):
        bundle, _, _ = self.bundle()
        damaged = self.root / 'damaged.zip'
        with zipfile.ZipFile(bundle) as original, zipfile.ZipFile(damaged, 'x') as output:
            for info in original.infolist():
                content = original.read(info.filename)
                if info.filename.startswith('private_uploads/'):
                    content = b'X' * len(content)
                output.writestr(info.filename, content)
        target = self.root / 'quarantined'
        with self.assertRaisesRegex(ValueError, '校验失败'):
            prepare_restore(damaged, target)
        self.assertFalse((target / 'READY.json').exists())

    def test_schema_checkpoint_and_future_version_rejected(self):
        from sqlalchemy import create_engine, MetaData, Table, Column, Integer
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        Table('synthetic', metadata, Column('id', Integer, primary_key=True))
        with engine.begin() as connection:
            metadata.create_all(connection)
            record_schema_revision(connection, metadata)
            self.assertEqual(guard_schema_revision(connection), CURRENT_SCHEMA_REVISION)
            connection.exec_driver_sql('PRAGMA user_version=' + str(CURRENT_SCHEMA_REVISION + 1))
            with self.assertRaisesRegex(ValueError, '高于当前程序'):
                guard_schema_revision(connection)
            connection.exec_driver_sql('PRAGMA user_version=' + str(CURRENT_SCHEMA_REVISION))
            connection.exec_driver_sql('CREATE TABLE unexpected_change(id INTEGER)')
            with self.assertRaisesRegex(ValueError, '结构与迁移账本不一致'):
                guard_schema_revision(connection)

    def test_archives_preserve_history_and_reject_writes_or_parent_deletes(self):
        from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String
        from sqlalchemy.exc import IntegrityError
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        Table('farms', metadata, Column('id', Integer, primary_key=True), Column('archived_at', String))
        Table('fields', metadata, Column('id', Integer, primary_key=True), Column('farm_id', Integer), Column('archived_at', String))
        for name in ('crop_plantings', 'farming_records', 'disease_records', 'weather_snapshots'):
            Table(name, metadata, Column('id', Integer, primary_key=True), Column('field_id', Integer))
        with engine.begin() as connection:
            metadata.create_all(connection)
            install_history_guards(connection)
            record_schema_revision(connection, metadata)
            self.assertEqual(guard_schema_revision(connection), CURRENT_SCHEMA_REVISION)
            connection.exec_driver_sql('INSERT INTO farms(id) VALUES(1)')
            connection.exec_driver_sql('INSERT INTO fields(id,farm_id) VALUES(2,1)')
            connection.exec_driver_sql('INSERT INTO crop_plantings(id,field_id) VALUES(3,2)')
            connection.exec_driver_sql('UPDATE farms SET archived_at=\'2026-10-14\' WHERE id=1')
            with self.assertRaisesRegex(IntegrityError, 'read only'):
                connection.exec_driver_sql('INSERT INTO farming_records(id,field_id) VALUES(4,2)')
            with self.assertRaisesRegex(IntegrityError, 'history is protected'):
                connection.exec_driver_sql('DELETE FROM fields WHERE id=2')
            self.assertEqual(connection.exec_driver_sql('SELECT count(*) FROM crop_plantings').scalar(), 1)
            connection.exec_driver_sql('UPDATE farms SET archived_at=NULL WHERE id=1')
            connection.exec_driver_sql('INSERT INTO farming_records(id,field_id) VALUES(4,2)')
            self.assertEqual(connection.exec_driver_sql('SELECT count(*) FROM farming_records').scalar(), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Main knowledge transfer/eligibility paths, using synthetic synchronous DBs only."""
from contextlib import contextmanager
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.core.model_base import Base
from app.models import KnowledgeItem
from app.services.knowledge_service import KnowledgeService
from app.services.rag_service import RAGService
from app.services.agri_retrieval_service import AgriRetrievalService
from app.schemas.knowledge import ImportKnowledgeRequest
from app.core.schema_revision import CURRENT_SCHEMA_REVISION
from tests.test_farming_submission_smoke import AsyncAdapter, finish_immediate


@contextmanager
def synthetic():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session, AsyncAdapter(session)
    finally: engine.dispose()


@pytest.fixture
def service(monkeypatch):
    service = KnowledgeService.__new__(KnowledgeService)
    async def no_external_vectors(*args, **kwargs): return False
    monkeypatch.setattr(service, '_sync_vector_add', no_external_vectors)
    monkeypatch.setattr(service, '_sync_vector_update', no_external_vectors)
    import app.api.knowledge as api
    monkeypatch.setattr(api, 'knowledge_service', service)
    return service


def test_export_import_preserves_metadata_and_quarantine_without_overwriting_source_id(service):
    from app.api.knowledge import export_knowledge, import_knowledge
    with synthetic() as (source, db):
        source.add_all([
            KnowledgeItem(id='existing-target', question='synthetic isolated source', answer='synthetic, no agronomic advice',
                category='synthetic', crop='synthetic-crop', region='synthetic-region', growth_stage='全生育期',
                topic='standard', disease='synthetic-target', source_type='quarantined', source_name='synthetic unreviewed source',
                authority_level='未核验', publish_date='2020-01-01', source_updated_at='2021-01-01',
                url='https://example.invalid/synthetic', version='source-v2', effective_from='2020-01-01',
                effective_to='2021-01-01', is_current=0, meta_info={'synthetic_review': 'not completed'}),
        ])
        source.commit()
        exported = finish_immediate(export_knowledge(db=db, current_user=None))
        item = exported.items[0]
        assert set(item) == set(KnowledgeItem.__table__.columns.keys())
        assert item['source_type'] == 'quarantined' and item['effective_to'] == '2021-01-01'
    payload = ImportKnowledgeRequest.model_validate({'items': [item, {'question': 'synthetic legacy minimal', 'answer': 'synthetic'}]})
    with synthetic() as (target, db):
        original = KnowledgeItem(id='existing-target', question='synthetic installed row', answer='keep original')
        target.add(original); target.commit()
        result = finish_immediate(import_knowledge(payload, db=db, current_user=None))
        assert result.success_count == 2 and result.skip_count == 0 and result.errors == []
        imported = target.scalar(select(KnowledgeItem).where(KnowledgeItem.question == item['question']))
        assert imported.id != item['id']
        for name in set(item) - {'id', 'created_at', 'updated_at'}:
            assert getattr(imported, name) == item[name], name
        legacy = target.scalar(select(KnowledgeItem).where(KnowledgeItem.question == 'synthetic legacy minimal'))
        assert (legacy.source_type, legacy.authority_level, legacy.version) == ('unverified', '未核验', None)
        assert target.get(KnowledgeItem, 'existing-target').answer == 'keep original'
        assert finish_immediate(service.filter_candidate_ids(db)) == []


def test_import_counts_duplicates_and_bad_dates_without_losing_successful_rows(service):
    with synthetic() as (session, db):
        session.add(KnowledgeItem(id='synthetic-existing', question='synthetic duplicate', answer='keep'))
        session.commit()
        items = [
            {'question': 'synthetic first valid', 'answer': 'synthetic'},
            {'question': 'synthetic invalid dates', 'answer': 'synthetic', 'effective_from': '2026-12-01', 'effective_to': '2026-01-01'},
            {'question': 'synthetic duplicate', 'answer': 'changed but skip'},
            {'question': 'synthetic second valid', 'answer': 'synthetic'},
        ]
        success, skipped, errors = finish_immediate(service.import_items(db, items, sync_vector=False))
        assert (success, skipped, len(errors)) == (2, 1, 1)
        assert errors[0].startswith('第2条:') and '日期' in errors[0]
        rows = session.scalars(select(KnowledgeItem)).all()
        assert len(rows) == 3 and all(row.question != 'synthetic invalid dates' for row in rows)
        assert session.get(KnowledgeItem, 'synthetic-existing').answer == 'keep'


def test_live_summary_and_both_retrieval_paths_share_version_date_and_source_gate(service, monkeypatch):
    from app.api.knowledge import get_knowledge_versions
    class Vectors:
        contract = {}
        async def sync_config_from_db(self, db): return False
        def compatibility(self): return False, 'synthetic offline'
        def get_index_info(self): return {}
    captured = []
    async def dense(self, queries, rows, threshold):
        captured.append({row['id'] for row in rows})
        return [(row, .95) for row in rows], {}
    monkeypatch.setattr(RAGService, '_get_vector_service', lambda self: Vectors())
    monkeypatch.setattr(RAGService, '_vector_search_multi', dense)
    monkeypatch.setattr(RAGService, '_bm25_search', lambda self, query, rows: {row['id']: 1.0 for row in rows})
    with synthetic() as (session, db):
        empty = finish_immediate(get_knowledge_versions(db=db, current_user=None))
        assert empty['status'] == 'empty' and empty['supported_crops'] == [] and empty['current_version'] is None
        defaults = dict(question='synthetic knowledge', answer='synthetic reference', crop='synthetic-crop',
            source_type='expert', authority_level='未核验', version='doc-v2', is_current=1)
        changes = {'eligible': {}, 'quarantined': {'source_type': 'quarantined'}, 'unverified': {'source_type': 'unverified'},
            'inactive': {'is_current': 0}, 'future': {'effective_from': '9999-01-01'},
            'expired': {'effective_to': '2000-01-01'}, 'bad-date': {'effective_to': '至今'}}
        rows = [KnowledgeItem(id=key, **{**defaults, **delta}) for key, delta in changes.items()]
        session.add_all(rows); session.commit()
        summary = finish_immediate(get_knowledge_versions(db=db, current_user=None))
        assert summary['current_version'] is None and summary['schema_version'] == CURRENT_SCHEMA_REVISION
        assert summary['candidate_items'] == 1 and summary['total_items'] == 7
        assert summary['supported_crops'] == ['synthetic-crop'] and summary['candidate_versions'] == ['doc-v2']
        assert finish_immediate(service.filter_candidate_ids(db)) == ['eligible']
        direct = finish_immediate(RAGService(use_rewrite=False).hybrid_search('synthetic', rows, threshold=.1))
        result, trace = finish_immediate(AgriRetrievalService.search(db, 'synthetic', {'crop': 'synthetic-crop'}, threshold=.1))
        assert captured == [{'eligible'}, {'eligible'}]
        assert [row['id'] for row in direct.retrieved_items] == [row['id'] for row in result.retrieved_items] == ['eligible']
        assert trace['eligible_candidates'] == 1 and result.retrieved_items[0]['source_verified'] is False
        blocked_only = finish_immediate(RAGService(use_rewrite=False).hybrid_search('synthetic', [row for row in rows if row.id != 'eligible']))
        assert blocked_only.retrieved_items == [] and blocked_only.need_expert
        assert len(captured) == 2  # No query embedding/search when all rows are excluded.

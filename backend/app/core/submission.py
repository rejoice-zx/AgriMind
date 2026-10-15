"""Canonical identity for one actual farming submission, not permission to change it."""
import hashlib
import json
from fastapi import HTTPException


def submission_fingerprint(field_id, payload):
    body = {key: value for key, value in payload.items() if key != 'request_id'}
    return hashlib.sha256(json.dumps({'field_id': field_id, 'payload': body},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def existing_submission(record, actor_subject, fingerprint):
    if record.created_by_subject != actor_subject or record.request_fingerprint != fingerprint:
        raise HTTPException(409, '同一提交编号不能用于不同账号或农事内容；请先核对履历，再明确新建另一笔记录')
    if record.revoked_at is not None:
        raise HTTPException(409, '原提交记录已撤销；请查看撤销档案，不会重复登记')
    record.replayed = True
    return record

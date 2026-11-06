"""Career administrator read models and scoped group management helpers."""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit

router = APIRouter(prefix='/career-management', tags=['Administración de carrera'])

def context(user):
    if user.get('role') != 'career_admin':
        raise HTTPException(403, detail='Solo el administrador de carrera puede utilizar esta sección')
    career = str(user.get('career_id') or '')
    if not ObjectId.is_valid(career):
        raise HTTPException(403, detail='La cuenta no tiene una carrera válida asignada')
    return get_database(), career

def career_filter(career):
    return {'career_id': {'$in': [career, ObjectId(career)]}, 'is_deleted': {'$ne': True}}

@router.get('/overview')
def overview(user=Depends(get_current_user)):
    db, career = context(user)
    scope = career_filter(career)
    career_doc = db.careers.find_one({'_id': ObjectId(career)}, {'name': 1})
    classes = db.classes.count_documents({**scope, 'is_active': {'$ne': False}})
    group_ids = [str(x['_id']) for x in db.groups.find(scope, {'_id': 1}).limit(500)]
    return {'success': True, 'data': {
        'career_name': (career_doc or {}).get('name', 'Mi carrera'),
        'groups': len(group_ids), 'classes': classes,
        'students': db.users.count_documents({**scope, 'role': 'student'}),
        'teachers': db.users.count_documents({**scope, 'role': 'teacher'}),
        'tutors': db.users.count_documents({**scope, 'role': 'tutor'}),
        'active_sessions': db.class_sessions.count_documents({**scope, 'status': 'active'}),
        'pending_justifications': db.justifications.count_documents({**scope, 'status': 'pending'}),
    }}

@router.get('/people')
def people(role: str = Query('student'), page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100), user=Depends(get_current_user)):
    db, career = context(user)
    if role not in {'student', 'teacher', 'tutor'}:
        raise HTTPException(422, detail='Rol no permitido')
    q = {**career_filter(career), 'role': role}
    docs = list(db.users.find(q, {'password_hash': 0, 'avatar': 0}).sort('full_name', 1).skip((page-1)*limit).limit(limit))
    result = []
    for doc in docs:
        item = {'id': str(doc['_id']), 'full_name': doc.get('full_name', 'Sin nombre'), 'email': doc.get('email', ''), 'role': role}
        if doc.get('avatar_path') or doc.get('avatar'):
            item['avatar_url'] = f"/api/v1/users/{doc['_id']}/avatar"
        elif doc.get('avatar_url'):
            item['avatar_url'] = doc['avatar_url']
        result.append(item)
    return {'success': True, 'data': {'items': result, 'total': db.users.count_documents(q), 'page': page}}

@router.get('/groups/{group_id}/members')
def group_members(group_id: str, user=Depends(get_current_user)):
    db, career = context(user)
    if not ObjectId.is_valid(group_id):
        raise HTTPException(400, detail='Grupo inválido')
    group = db.groups.find_one({'_id': ObjectId(group_id), **career_filter(career)})
    if not group:
        raise HTTPException(404, detail='Grupo no encontrado en tu carrera')
    refs = [group_id, ObjectId(group_id)]
    profiles = list(db.students.find({'group_id': {'$in': refs}, 'is_deleted': {'$ne': True}}, {'user_id': 1}).limit(500))
    ids = [ObjectId(str(p['user_id'])) for p in profiles if ObjectId.is_valid(str(p.get('user_id', '')))]
    users = list(db.users.find({'_id': {'$in': ids}, **career_filter(career)}, {'full_name': 1, 'email': 1, 'avatar_path': 1}))
    return {'success': True, 'data': {'group': serialize(group), 'students': [
        {'id': str(u['_id']), 'full_name': u.get('full_name', ''), 'email': u.get('email', ''), 'avatar_url': f"/api/v1/users/{u['_id']}/avatar" if u.get('avatar_path') else None} for u in users]}}

@router.post('/groups/{group_id}/tutor')
def assign_tutor(group_id: str, payload: dict, user=Depends(get_current_user)):
    db, career = context(user)
    tutor_id = str(payload.get('tutor_id', ''))
    if not ObjectId.is_valid(group_id) or not ObjectId.is_valid(tutor_id):
        raise HTTPException(422, detail='Grupo o tutor inválido')
    if not db.groups.find_one({'_id': ObjectId(group_id), **career_filter(career)}):
        raise HTTPException(404, detail='Grupo no encontrado')
    tutor = db.tutors.find_one({'$or': [{'_id': ObjectId(tutor_id)}, {'user_id': tutor_id}], **career_filter(career)})
    if not tutor:
        raise HTTPException(404, detail='Tutor no encontrado en tu carrera')
    now = datetime.now(timezone.utc)
    db.tutor_assignments.update_one({'group_id': group_id, 'tutor_id': str(tutor['_id'])}, {'$set': {'is_active': True, 'career_id': career, 'updated_at': now}, '$setOnInsert': {'created_at': now}}, upsert=True)
    audit(db, user, 'assign_tutor', 'groups', group_id)
    return {'success': True, 'data': {'group_id': group_id, 'tutor_id': str(tutor['_id'])}}

"""Career administrator read models and scoped group management helpers."""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit
from app.routers.academic.classes import id_refs, managed_career, tutor_group_ids
from app.services.academic_catalogs import INSTITUTION_ADMINS, catalog_scope

router = APIRouter(prefix='/career-management', tags=['Administración de carrera'])

def context(user):
    if user.get('role') != 'career_admin':
        raise HTTPException(403, detail='Solo el administrador de carrera puede utilizar esta sección')
    career = str(user.get('career_id') or '')
    if not ObjectId.is_valid(career):
        raise HTTPException(403, detail='La cuenta no tiene una carrera válida asignada')
    db = get_database()
    if user.get('institution_id'):
        managed_career(db, user, career)
    return db, career

def career_filter(career):
    return {'career_id': {'$in': [career, ObjectId(career)]}, 'is_deleted': {'$ne': True}}


def people_filter(db, user, career, role):
    if not user.get('institution_id'):
        return {**career_filter(career), 'role': role}
    institution_id = user['institution_id']
    members = db.institution_memberships.find({'institution_id': institution_id, 'status': 'active'}, {'user_id': 1})
    member_ids = {str(item['user_id']) for item in members if ObjectId.is_valid(str(item.get('user_id')))}
    assignments = db.role_assignments.find({
        'institution_id': institution_id, 'role': role, 'status': 'active',
        '$or': [{'career_id': {'$in': id_refs(career)}},
                {'scope_type': 'career', 'scope_id': {'$in': id_refs(career)}}],
    }, {'user_id': 1})
    assigned_ids = {str(item['user_id']) for item in assignments} & member_ids
    if role == 'student':
        return {'is_active': True, '$or': [
            {'_id': {'$in': [ObjectId(value) for value in assigned_ids]}},
            {'_id': {'$in': [ObjectId(value) for value in member_ids]},
             'role': 'student', **career_filter(career)},
        ]}
    return {'is_active': True, '_id': {'$in': [ObjectId(value) for value in assigned_ids]}}


@router.get('/overview')
def overview(user=Depends(get_current_user)):
    db, career = context(user)
    scope = career_filter(career)
    if user.get('institution_id'):
        scope['institution_id'] = {'$in': [None, user['institution_id']]}
    career_doc = db.careers.find_one({'_id': ObjectId(career)}, {'name': 1})
    classes = db.classes.count_documents({**scope, 'is_active': {'$ne': False}})
    groups = db.groups.count_documents(scope)
    return {'success': True, 'data': {
        'career_id': career, 'career_name': (career_doc or {}).get('name', 'Mi carrera'),
        'groups': groups, 'classes': classes,
        'students': db.users.count_documents(people_filter(db, user, career, 'student')),
        'teachers': db.users.count_documents(people_filter(db, user, career, 'teacher')),
        'tutors': db.users.count_documents(people_filter(db, user, career, 'tutor')),
        'active_sessions': db.class_sessions.count_documents({**scope, 'status': 'active'}),
        'pending_justifications': db.justifications.count_documents({**scope, 'status': 'pending'}),
    }}

@router.get('/people')
def people(role: str = Query('student'), page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100), search: str | None = Query(None, max_length=80), user=Depends(get_current_user)):
    db, career = context(user)
    if role not in {'student', 'teacher', 'tutor'}:
        raise HTTPException(422, detail='Rol no permitido')
    q = people_filter(db, user, career, role)
    if search and search.strip():
        from re import escape
        pattern = {"$regex": escape(search.strip()), "$options": "i"}
        q = {"$and": [q, {"$or": [{"full_name": pattern}, {"email": pattern}]}]}
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
    institutional_admin = bool(user.get('institution_id') and user.get('role') in INSTITUTION_ADMINS)
    if institutional_admin:
        db = get_database()
        filters = catalog_scope(db, user, 'groups')
    elif user.get('role') == 'tutor':
        db = get_database()
        career = managed_career(db, user, None)
    else:
        db, career = context(user)
    if not institutional_admin:
        filters = career_filter(career)
        if user.get('institution_id'):
            filters['institution_id'] = {'$in': [user['institution_id'], None]}
    if not ObjectId.is_valid(group_id):
        raise HTTPException(400, detail='Grupo inválido')
    group = db.groups.find_one({'_id': ObjectId(group_id), **filters})
    if not group or (user['role'] == 'tutor' and str(group['_id']) not in tutor_group_ids(db, user)):
        raise HTTPException(404, detail='Grupo no encontrado en tu carrera')
    if institutional_admin:
        career = str(group['career_id'])
    group_query = {'group_id': {'$in': id_refs(group['_id'])}, 'is_deleted': {'$ne': True}}
    if user.get('institution_id'):
        group_query['institution_id'] = {'$in': [None, user['institution_id']]}
    profiles = db.students.find(group_query, {'user_id': 1})
    profile_ids = {ObjectId(str(p['user_id'])) for p in profiles if ObjectId.is_valid(str(p.get('user_id', '')))}
    class_filters = {**group_query, **career_filter(career)}
    if user.get('institution_id'):
        class_filters['institution_id'] = {'$in': [user['institution_id'], None]}
    classes = db.classes.find(class_filters, {'_id': 1})
    class_refs = [ref for item in classes for ref in id_refs(item['_id'])]
    memberships = db.class_memberships.find({'class_id': {'$in': class_refs}}, {'student_id': 1})
    member_ids = {ObjectId(str(m['student_id'])) for m in memberships if ObjectId.is_valid(str(m.get('student_id', '')))}
    people_query = {
        'is_deleted': {'$ne': True},
        '$or': [
            {'_id': {'$in': list(profile_ids)}, 'career_id': {'$in': id_refs(career)}},
            {'_id': {'$in': list(member_ids)}},
        ],
    }
    if user.get('institution_id'):
        memberships = db.institution_memberships.find({
            'institution_id': user['institution_id'], 'status': 'active',
            'user_id': {'$in': [ref for identifier in profile_ids | member_ids for ref in id_refs(identifier)]},
        }, {'user_id': 1})
        people_query['_id'] = {'$in': [ObjectId(str(member['user_id'])) for member in memberships
                                       if ObjectId.is_valid(str(member.get('user_id', '')))]}
    users = db.users.find(people_query, {'full_name': 1, 'email': 1, 'avatar_path': 1}).sort('full_name', 1)
    return {'success': True, 'data': {'group': serialize(group), 'students': [
        {'id': str(u['_id']), 'full_name': u.get('full_name', ''), 'email': u.get('email', ''), 'avatar_url': f"/api/v1/users/{u['_id']}/avatar" if u.get('avatar_path') else None} for u in users]}}

@router.post('/groups/{group_id}/tutor')
def assign_tutor(group_id: str, payload: dict, user=Depends(get_current_user)):
    db, career = context(user)
    tutor_id = str(payload.get('tutor_id', ''))
    if not ObjectId.is_valid(group_id) or not ObjectId.is_valid(tutor_id):
        raise HTTPException(422, detail='Grupo o tutor inválido')
    group_id = str(ObjectId(group_id))
    filters = catalog_scope(db, user, 'groups') if user.get('institution_id') else career_filter(career)
    if not db.groups.find_one({'_id': ObjectId(group_id), **filters}):
        raise HTTPException(404, detail='Grupo no encontrado')
    tutor = db.tutors.find_one({'$or': [{'_id': ObjectId(tutor_id)}, {'user_id': {'$in': id_refs(tutor_id)}}],
                               'is_active': {'$ne': False}, **career_filter(career),
                               **({'institution_id': {'$in': [None, user['institution_id']]}} if user.get('institution_id') else {})})
    if user.get('institution_id'):
        from app.routers.institutions import assign_role

        target = tutor_id
        if tutor:
            target = str(tutor['user_id'])
        assign_role(user['institution_id'], {
            'user_id': target, 'role': 'tutor', 'scope_type': 'group', 'scope_id': group_id,
        }, user)
        return {'success': True, 'data': {'group_id': group_id, 'tutor_id': str(tutor['_id']) if tutor else target}}
    if not tutor:
        raise HTTPException(404, detail='Tutor no encontrado en tu carrera')
    now = datetime.now(timezone.utc)
    db.tutor_assignments.update_one(
        {'group_id': group_id, 'tutor_id': str(tutor['_id'])},
        {'$set': {'is_active': True, 'is_deleted': False, 'career_id': career, 'updated_at': now},
         '$setOnInsert': {'created_at': now}},
        upsert=True,
    )
    audit(db, user, 'assign_tutor', 'groups', group_id)
    return {'success': True, 'data': {'group_id': group_id, 'tutor_id': str(tutor['_id'])}}

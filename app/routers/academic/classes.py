"""Clases de inscripción: distintas de las sesiones temporales de asistencia."""
from datetime import datetime, timezone
import secrets
import string
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import DuplicateKeyError
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit
from app.services.institutions import institution_career_refs
from app.services.academic_catalogs import INSTITUTION_ADMINS, catalog_scope

router = APIRouter(prefix='/classes', tags=['Clases e inscripción'])
MANAGERS = {'admin', 'super_admin', 'career_admin', 'tutor', 'teacher'}
GENERATED_CODE_CHARACTERS = string.ascii_uppercase + string.digits
JOIN_CODE_CHARACTERS = string.ascii_letters + string.digits


def generate_join_code():
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        *(secrets.choice(GENERATED_CODE_CHARACTERS) for _ in range(4)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return ''.join(characters)


def fail(status, code, message):
    raise HTTPException(status, detail={'code': code, 'message': message})


def database():
    db = get_database()
    if db is None:
        fail(503, 'DATABASE_UNAVAILABLE', 'Base de datos no disponible')
    return db


def oid(value, label):
    if not isinstance(value, str) or not ObjectId.is_valid(value):
        fail(422, 'INVALID_ID', f'Selecciona un {label} válido')
    return ObjectId(value)


def managed_career(db, user, requested):
    role = user['role']
    if role in {'admin', 'super_admin'}:
        cid = requested
    elif role == 'career_admin' or (role in {'teacher', 'tutor'} and user.get('institution_id')):
        cid = user.get('career_id')
    elif role in {'tutor', 'teacher'}:
        profile = db['tutors' if role == 'tutor' else 'teachers'].find_one({'user_id': str(user['_id']), 'is_active': {'$ne': False}, 'is_deleted': {'$ne': True}})
        cid = (profile or {}).get('career_id') or user.get('career_id')
    else:
        fail(403, 'FORBIDDEN', 'No puedes administrar clases')
    if not cid:
        fail(403, 'SCOPE_MISSING', 'Tu cuenta no tiene una carrera autorizada')
    if requested and role not in {'admin', 'super_admin'} and str(requested) != str(cid):
        fail(403, 'FORBIDDEN', 'No puedes administrar otra carrera')
    career_query={'_id': oid(str(cid), 'carrera'), 'is_deleted': {'$ne': True}}
    if user.get('institution_id'):
        career_query['institution_id']=user['institution_id']
    if not db.careers.find_one(career_query):
        fail(422, 'CAREER_NOT_FOUND', 'Carrera inexistente')
    return str(cid)


def id_refs(value):
    value = str(value)
    return [value, ObjectId(value)] if ObjectId.is_valid(value) else [value]


def assigned_tutor_ids(db, cls, institution_id=None):
    group = str(cls.get('group_id', ''))
    if institution_id:
        assignments = db.role_assignments.find({
            'institution_id': institution_id, 'role': 'tutor', 'scope_type': 'group',
            'scope_id': {'$in': id_refs(group)}, 'status': 'active',
        }, {'user_id': 1})
        candidates = [ref for row in assignments for ref in id_refs(row.get('user_id'))]
        memberships = db.institution_memberships.find({
            'institution_id': institution_id, 'status': 'active', 'user_id': {'$in': candidates},
        }, {'user_id': 1})
        refs = {str(row['user_id']) for row in memberships}
    else:
        refs = set(map(str, cls.get('tutor_ids') or []))
        for assignment in db.tutor_assignments.find({
            'group_id': {'$in': id_refs(group)}, 'is_active': {'$ne': False},
            'is_deleted': {'$ne': True},
        }):
            ref = str(assignment.get('tutor_id', ''))
            if ObjectId.is_valid(ref):
                profile = db.tutors.find_one({'_id': ObjectId(ref)})
                if not profile or (profile.get('is_active') is not False and not profile.get('is_deleted')):
                    refs.add(str((profile or {}).get('user_id', ref)))
    ids = [ObjectId(ref) for ref in refs if ObjectId.is_valid(ref)]
    return sorted(row['_id'] for row in db.users.find({
        '_id': {'$in': ids}, 'is_active': {'$ne': False}, 'is_deleted': {'$ne': True},
    }, {'_id': 1}))


def institutional_class_scope(db, user):
    return {
        'career_id': {'$in': institution_career_refs(db, user)},
        'institution_id': {'$in': [user['institution_id'], None]},
        'is_deleted': {'$ne': True},
    }


def tutor_group_ids(db, user):
    """Asignaciones de grupo explícitas; no confundir tutor con profesor de materia."""
    uid = str(user['_id'])
    if user.get('institution_id'):
        return list({
            str(row["scope_id"]) for row in db.role_assignments.find({
                "institution_id": user["institution_id"], "user_id": {"$in": id_refs(uid)},
                "role": "tutor", "scope_type": "group", "career_id": {"$in": id_refs(user.get("career_id"))}, "status": "active",
            }, {"scope_id": 1}) if ObjectId.is_valid(str(row.get("scope_id")))
        })
    profile = db.tutors.find_one({'user_id': {'$in': id_refs(uid)}, 'is_active': {'$ne': False}, 'is_deleted': {'$ne': True}})
    refs = id_refs(uid)
    if profile:
        refs.extend(id_refs(profile['_id']))
    return list({str(x['group_id']) for x in db.tutor_assignments.find({
        'tutor_id': {'$in': refs}, 'group_id': {'$exists': True},
        'is_active': {'$ne': False}, 'is_deleted': {'$ne': True}
    }) if x.get('group_id')})


def scope(db, user):
    role = user['role']
    uid = str(user['_id'])
    if role in {'admin', 'super_admin'}:
        return {}
    if role in INSTITUTION_ADMINS:
        return institutional_class_scope(db, user)
    if role == 'career_admin':
        career = managed_career(db, user, None)
        return institutional_class_scope(db, user) if user.get('institution_id') else {
            'career_id': {'$in': id_refs(career)}, 'is_deleted': {'$ne': True}}
    if role == 'tutor':
        career = managed_career(db, user, None)
        groups = [ref for group in tutor_group_ids(db, user) for ref in id_refs(group)]
        query = {'career_id': {'$in': id_refs(career)}, 'group_id': {'$in': groups}, 'is_deleted': {'$ne': True}}
        if user.get('institution_id'):
            query['institution_id'] = {'$in': [user['institution_id'], None]}
        if not user.get('institution_id'):
            query.pop('group_id')
            query['$or'] = [{'group_id': {'$in': groups}}, {'tutor_ids': {'$in': id_refs(uid)}}]
        return query
    if role == 'teacher':
        query={'is_deleted': {'$ne': True}, 'teacher_id': {'$in': [uid, ObjectId(uid)]} if ObjectId.is_valid(uid) else uid}
        if user.get('institution_id'):
            query.update(institutional_class_scope(db, user))
            context = user.get('active_context') or {}
            if context.get('scope_type') == 'class':
                query['_id'] = {'$in': [ref for ref in id_refs(context.get('scope_id')) if isinstance(ref, ObjectId)]}
        return query
    if role == 'student':
        memberships = db.class_memberships.find({'student_id': {'$in': id_refs(uid)}}, {'class_id': 1})
        query = {'_id': {'$in': [ObjectId(str(x['class_id'])) for x in memberships
                               if ObjectId.is_valid(str(x.get('class_id')))]}}
        if user.get('institution_id'):
            query.update(institutional_class_scope(db, user))
            if not user.get('career_id'):
                query['career_id'] = {'$in': []}
        return query
    fail(403, 'FORBIDDEN', 'Sin permisos')


def class_details(db, doc):
    row = serialize(doc)
    for field, collection, output in (
        ('subject_id', 'subjects', 'subject_name'),
        ('group_id', 'groups', 'group_name'),
        ('period_id', 'academic_periods', 'period_name'),
    ):
        value = str(doc.get(field, ''))
        record = db[collection].find_one({'_id': ObjectId(value)}, {'name': 1, 'code': 1}) if ObjectId.is_valid(value) else None
        row[output] = (record or {}).get('name') or (record or {}).get('code') or 'Sin asignar'
    value = str(doc.get('teacher_id', ''))
    teacher = db.users.find_one({'_id': ObjectId(value)}, {'full_name': 1}) if ObjectId.is_valid(value) else None
    row['teacher_name'] = (teacher or {}).get('full_name') or 'Docente'
    return row


def validate_relations(db, payload, career, actor):
    institutional = bool(actor.get('institution_id'))
    for field, coll in [('subject_id', 'subjects'), ('group_id', 'groups'), ('period_id', 'academic_periods')]:
        value = payload.get(field)
        if not value:
            fail(422, 'REQUIRED_FIELD', f'Falta {field}')
        filters = catalog_scope(db, {**actor, 'career_id': career}, coll) if institutional else {'is_deleted': {'$ne': True}}
        doc = db[coll].find_one({'_id': oid(value, field), **filters})
        if not doc or (doc.get('career_id') and str(doc['career_id']) != career):
            fail(422, 'INVALID_RELATION', f'{field} no pertenece a la carrera seleccionada')
        if institutional and field == 'group_id' and doc.get('period_id') and str(doc['period_id']) != str(payload.get('period_id')):
            fail(422, 'INVALID_PERIOD', 'Selecciona el periodo académico del grupo')
    teacher_id = payload.get('teacher_id')
    teacher_query = {'_id': oid(teacher_id, 'docente'), 'is_deleted': {'$ne': True}, 'is_active': {'$ne': False}}
    if institutional:
        teacher_query.update(career_id={'$in': id_refs(career)},
                             institution_id={'$in': [actor['institution_id'], None]})
    teacher = db.teachers.find_one(teacher_query)
    if not teacher or (not institutional and teacher.get('career_id') and str(teacher['career_id']) != career):
        fail(422, 'INVALID_TEACHER', 'El docente no pertenece a la carrera')
    account_query = {'_id': oid(str(teacher['user_id']), 'usuario docente'), 'is_active': {'$ne': False}}
    if institutional:
        assignment = db.role_assignments.find_one({
            'institution_id': actor['institution_id'], 'user_id': {'$in': id_refs(teacher['user_id'])},
            'role': 'teacher', 'scope_type': 'career', 'scope_id': career, 'status': 'active',
        })
        if not assignment:
            fail(422, 'INVALID_TEACHER', 'Asigna primero el rol de docente en esta carrera')
    else:
        account_query['role'] = 'teacher'
    user = db.users.find_one(account_query)
    if not user:
        fail(422, 'INVALID_TEACHER', 'El perfil docente no tiene una cuenta activa')
    return str(user['_id'])


def ensure_student_class_scope(db, user, class_doc):
    career_id = user.get('career_id')
    institution_id = user.get('institution_id')
    if institution_id:
        if class_doc.get('institution_id') not in (None, institution_id):
            fail(403, 'CAREER_INSTITUTION_MISMATCH', 'La clase no pertenece a tu institución')
        if not career_id:
            fail(403, 'CAREER_REQUIRED', 'Tu cuenta no tiene una carrera autorizada')
        career = db.careers.find_one({
            '_id': oid(str(career_id), 'carrera'),
            'institution_id': institution_id,
            'is_deleted': {'$ne': True},
        })
        if not career:
            fail(403, 'CAREER_INSTITUTION_MISMATCH', 'Tu carrera no pertenece a tu institución')
    if career_id and str(career_id) != str(class_doc.get('career_id')):
        fail(403, 'CAREER_MISMATCH', 'La clase pertenece a otra carrera')


@router.get('/options')
def class_options(user=Depends(get_current_user)):
    """Catálogos de clase con permisos de lectura, sin exponer el CRUD administrativo."""
    db = database()
    role = user.get('role')
    if role not in MANAGERS:
        fail(403, 'FORBIDDEN', 'Sin permisos para administrar clases')
    career = None if role in {'admin', 'super_admin'} else managed_career(db, user, None)
    def records(collection):
        query = {'is_deleted': {'$ne': True}, 'is_active': {'$ne': False}}
        if user.get('institution_id') and collection in {'subjects', 'groups', 'academic_periods'}:
            query.update(catalog_scope(db, {**user, 'career_id': career}, collection))
        elif user.get('institution_id') and collection == 'teachers':
            assignments = db.role_assignments.find({
                'institution_id': user['institution_id'], 'role': 'teacher',
                'scope_type': 'career', 'scope_id': career, 'status': 'active',
            }, {'user_id': 1})
            query['user_id'] = {'$in': [ref for item in assignments for ref in id_refs(item['user_id'])]}
            query['career_id'] = {'$in': id_refs(career)}
            query['institution_id'] = {'$in': [user['institution_id'], None]}
        elif career:
            # Los periodos pueden ser institucionales y no tener carrera.
            query['$or'] = [{'career_id': career}, {'career_id': {'$exists': False}}] if collection == 'academic_periods' else [{'career_id': career}]
        return [serialize(doc) for doc in db[collection].find(query).limit(300)]
    teachers = [] if role == 'teacher' else records('teachers')
    allowed_groups = set(tutor_group_ids(db, user)) if role == 'tutor' else None
    for teacher in teachers:
        uid = teacher.get('user_id')
        account = db.users.find_one({'_id': ObjectId(uid)}, {'full_name': 1, 'email': 1}) if isinstance(uid, str) and ObjectId.is_valid(uid) else None
        teacher['full_name'] = account.get('full_name', '') if account else ''
    return {'success': True, 'data': {
        'careers': records('careers') if career is None else [serialize(doc) for doc in db.careers.find({'_id': ObjectId(career)})],
        'subjects': records('subjects'), 'groups': [g for g in records('groups') if allowed_groups is None or str(g.get('id') or g.get('_id')) in allowed_groups],
        'academic-periods': records('academic_periods'), 'teachers': teachers,
        'assigned_career_id': career,
        'tutors': [dict(serialize(p), full_name=(db.users.find_one({'_id': ObjectId(p['user_id'])}, {'full_name': 1}) or {}).get('full_name', 'Tutor')) for p in db.tutors.find({'career_id': career, 'is_deleted': {'$ne': True}}) if ObjectId.is_valid(str(p.get('user_id', '')))] if career and not user.get('institution_id') else [],
    }}


@router.post('/groups', status_code=201)
def create_tutor_group(payload: dict, user=Depends(get_current_user)):
    if user['role'] != 'tutor':
        fail(403, 'FORBIDDEN', 'Solo tutores')
    from app.services.academic_catalogs import validate_group_period

    db = database()
    career = managed_career(db, user, None)
    name = str(payload.get('name') or '').strip()
    code = str(payload.get('code') or '').strip()
    if not 1 <= len(name) <= 120 or not 1 <= len(code) <= 40:
        fail(422, 'INVALID_GROUP', 'Indica el nombre y clave del grupo')
    institution = user.get('institution_id')
    uid = str(user['_id'])
    period = payload.get('period_id')
    now = datetime.now(timezone.utc)
    group = {'_id': ObjectId(), 'name': name, 'code': code, 'period_id': period, 'career_id': career,
             'institution_id': institution, 'created_at': now, 'updated_at': now, 'created_by': uid}
    if institution:
        validate_group_period(db, user, group, require_active=True)
    elif not db.academic_periods.find_one({
        '_id': oid(period, 'periodo'), 'institution_id': None,
        'career_id': {'$in': [*id_refs(career), None]}, 'is_active': {'$ne': False}, 'is_deleted': {'$ne': True},
    }):
        fail(422, 'INVALID_PERIOD', 'Selecciona un periodo disponible para tu carrera')
    if db.groups.find_one({'institution_id': institution, 'career_id': career, 'period_id': period,
                           'code': code, 'is_deleted': {'$ne': True}}):
        fail(409, 'DUPLICATE_GROUP', 'Esta clave ya existe en el periodo')
    db.groups.insert_one(group)
    try:
        if institution:
            db.role_assignments.insert_one({
                'institution_id': institution, 'user_id': uid, 'role': 'tutor', 'scope_type': 'group',
                'scope_id': str(group['_id']), 'career_id': career, 'status': 'active', 'granted_by': uid, 'granted_at': now,
            })
        else:
            db.tutor_assignments.insert_one({
                'tutor_id': uid, 'group_id': str(group['_id']), 'career_id': career,
                'is_active': True, 'created_at': now,
            })
    except Exception:
        db.groups.update_one({'_id': group['_id']}, {'$set': {'is_deleted': True}})
        raise
    audit(db, user, 'create', 'groups', str(group['_id']))
    return {'success': True, 'data': serialize(group)}


@router.post('', status_code=201)
def create_class(payload: dict, user=Depends(get_current_user)):
    if user['role'] not in MANAGERS:
        fail(403, 'FORBIDDEN', 'Sin permisos para crear clases')
    db = database()
    career = managed_career(db, user, payload.get('career_id'))
    if user['role'] == 'tutor' and str(payload.get('group_id')) not in tutor_group_ids(db, user):
        fail(403, 'GROUP_NOT_ASSIGNED', 'No tienes asignado este grupo')
    if user['role'] == 'teacher':
        profile_query = {'user_id': {'$in': id_refs(user['_id'])}, 'is_deleted': {'$ne': True}}
        if user.get('institution_id'):
            profile_query.update(career_id={'$in': id_refs(career)},
                                 institution_id={'$in': [user['institution_id'], None]})
        profile = db.teachers.find_one(profile_query)
        if not profile:
            fail(403, 'TEACHER_PROFILE_MISSING', 'Tu cuenta no tiene perfil docente')
        payload['teacher_id'] = str(profile['_id'])
    teacher_user_id = validate_relations(db, payload, career, user)
    now = datetime.now(timezone.utc)
    data = {'career_id': career, 'subject_id': payload['subject_id'], 'group_id': payload['group_id'],
            'period_id': payload['period_id'], 'teacher_id': teacher_user_id,
            'enrollment_open': True, 'is_active': True, 'created_at': now, 'updated_at': now,
            'created_by': str(user['_id']), 'tutor_ids': []}
    if user.get('institution_id'):
        data['institution_id'] = user['institution_id']
    for _ in range(15):
        data['join_code'] = generate_join_code()
        try:
            result = db.classes.insert_one(data)
            data['_id'] = result.inserted_id
            audit(db, user, 'create', 'classes', str(result.inserted_id))
            return {'success': True, 'data': serialize(data)}
        except DuplicateKeyError:
            if db.classes.find_one({'career_id': career, 'subject_id': data['subject_id'], 'group_id': data['group_id'], 'period_id': data['period_id'], 'teacher_id': teacher_user_id, 'is_active': True}):
                fail(409, 'DUPLICATE_CLASS', 'Esta clase ya está registrada')
    fail(503, 'CODE_UNAVAILABLE', 'No fue posible generar un código único')


@router.get('')
def list_classes(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                 career_id: str | None = None, group_id: str | None = None, period_id: str | None = None,
                 search: str | None = Query(None, max_length=80), user=Depends(get_current_user)):
    from re import escape
    db = database()
    query = scope(db, user)
    query['is_active'] = {'$ne': False}
    filters = [query]
    for key, value in (('career_id', career_id), ('group_id', group_id), ('period_id', period_id)):
        if value:
            filters.append({key: {'$in': id_refs(oid(value, key))}})
    if search and search.strip():
        term = {'$regex': escape(search.strip()), '$options': 'i'}
        any_match = []
        for field, collection, name in (('subject_id', 'subjects', 'name'), ('group_id', 'groups', 'name'),
                                         ('teacher_id', 'users', 'full_name'), ('period_id', 'academic_periods', 'name')):
            rows = db[collection].find({name: term}, {'_id': 1})
            any_match.append({field: {'$in': [ref for row in rows for ref in id_refs(row['_id'])]}})
        filters.append({'$or': any_match})
    query = {'$and': filters} if len(filters) > 1 else query
    total = db.classes.count_documents(query)
    raw = list(db.classes.find(query).sort('created_at', -1).skip((page-1)*limit).limit(limit))
    lookup = {}
    for field, collection in (('subject_id','subjects'),('group_id','groups'),('period_id','academic_periods'),('teacher_id','users')):
        ids = [ObjectId(str(d[field])) for d in raw if ObjectId.is_valid(str(d.get(field,'')))]
        lookup[field] = {str(d['_id']): d for d in db[collection].find({'_id': {'$in': ids}}, {'name':1,'code':1,'full_name':1})} if ids else {}
    docs = []
    for d in raw:
        item = serialize(d)
        for field, target in (('subject_id','subject_name'),('group_id','group_name'),('period_id','period_name'),('teacher_id','teacher_name')):
            ref = lookup[field].get(str(d.get(field)), {})
            item[target] = ref.get('name') or ref.get('full_name') or ref.get('code') or 'Sin asignar'
        docs.append(item)
    if user['role'] == 'student':
        for d in docs:
            d.pop('join_code', None)
    return {'success': True, 'data': docs, 'meta': {'page': page, 'limit': limit, 'total': total}}


@router.post('/join')
def join_class(payload: dict, user=Depends(get_current_user)):
    if user['role'] != 'student':
        fail(403, 'FORBIDDEN', 'Solo los alumnos pueden inscribirse con código')
    code = str(payload.get('code', '')).strip()
    if len(code) != 6 or any(c not in JOIN_CODE_CHARACTERS for c in code):
        fail(422, 'INVALID_CODE', 'El código debe tener seis caracteres entre letras y números')
    code = code.upper()
    db = database()
    doc = db.classes.find_one({'join_code': code, 'is_active': True, 'enrollment_open': True})
    if not doc:
        fail(404, 'CLASS_NOT_FOUND', 'Código no válido o inscripciones cerradas')
    ensure_student_class_scope(db, user, doc)
    now = datetime.now(timezone.utc)
    # Un alumno puede cursar distintas materias, pero no dos grupos de la misma
    # materia durante el mismo periodo.
    class_refs = id_refs(doc['_id'])
    for entry in db.class_memberships.find({'student_id': {'$in': id_refs(user['_id'])}}):
        existing_id = entry.get('class_id')
        if existing_id in class_refs:
            fail(409, 'ALREADY_ENROLLED', 'Ya estás inscrito en esta clase')
        existing = db.classes.find_one({'_id': {'$in': id_refs(existing_id)}})
        if existing and str(existing.get('subject_id')) == str(doc['subject_id']) and str(existing.get('period_id')) == str(doc['period_id']):
            fail(409, 'SUBJECT_ALREADY_ENROLLED', 'Ya estás inscrito en esta materia durante el periodo')
    membership = {'class_id': doc['_id'], 'student_id': str(user['_id'])}
    subject_period_key = f"{doc['subject_id']}:{doc['period_id']}"
    try:
        result = db.class_memberships.update_one(membership, {'$setOnInsert': {
            **membership, 'subject_period_key': subject_period_key, 'joined_at': now}}, upsert=True)
    except DuplicateKeyError:
        fail(409, 'ALREADY_ENROLLED', 'Ya estás inscrito en esta clase o materia')
    if result.upserted_id is None:
        fail(409, 'ALREADY_ENROLLED', 'Ya estás inscrito en esta clase')
    audit(db, user, 'join', 'classes', str(doc['_id']))
    return {'success': True, 'data': {'class_id': str(doc['_id']), 'message': 'Inscripción registrada'}}


@router.get('/invitations/mine')
def my_invitations(user=Depends(get_current_user)):
    if user['role'] != 'tutor':
        fail(403, 'FORBIDDEN', 'Solo tutores')
    db = database()
    items = [serialize(x) for x in db.class_tutor_invitations.find({'tutor_id': str(user['_id']), 'status': 'pending'})]
    return {'success': True, 'data': items}


@router.post('/invitations/{invitation_id}/accept')
def accept_invitation(invitation_id: str, user=Depends(get_current_user)):
    if user['role'] != 'tutor':
        fail(403, 'FORBIDDEN', 'Solo tutores')
    db = database()
    invite = db.class_tutor_invitations.find_one({'_id': oid(invitation_id, 'invitación'),
        'tutor_id': str(user['_id']), 'status': 'pending'})
    if not invite:
        fail(404, 'NOT_FOUND', 'Invitación no encontrada')
    db.classes.update_one({'_id': invite['class_id']}, {'$addToSet': {'tutor_ids': str(user['_id'])}})
    db.class_tutor_invitations.update_one({'_id': invite['_id']}, {'$set': {'status': 'accepted', 'accepted_at': datetime.now(timezone.utc)}})
    return {'success': True, 'data': {'message': 'Invitación aceptada'}}

@router.patch('/{class_id}')
def update_class(class_id: str, payload: dict, user=Depends(get_current_user)):
    if user['role'] not in MANAGERS:
        fail(403, 'FORBIDDEN', 'Sin permisos')
    db = database()
    query = {'$and': [{'_id': oid(class_id, 'clase')}, scope(db, user)]}
    doc = db.classes.find_one(query)
    if not doc:
        fail(404, 'NOT_FOUND', 'Clase no encontrada')
    changes = {}
    for key in ('enrollment_open', 'is_active'):
        if key in payload:
            if not isinstance(payload[key], bool):
                fail(422, 'INVALID_VALUE', f'{key} debe ser booleano')
            changes[key] = payload[key]
    if 'teacher_id' in payload:
        changes['teacher_id'] = validate_relations(db, {**doc, 'teacher_id': payload['teacher_id']}, str(doc['career_id']), user)
    if payload.get('regenerate_code') is True:
        for _ in range(15):
            code = generate_join_code()
            if not db.classes.find_one({'join_code': code}):
                changes['join_code'] = code
                break
        if 'join_code' not in changes:
            fail(503, 'CODE_UNAVAILABLE', 'No fue posible regenerar el código')
    if not changes:
        fail(422, 'EMPTY_DATA', 'Sin cambios')
    changes['updated_at'] = datetime.now(timezone.utc)
    db.classes.update_one(query, {'$set': changes})
    audit(db, user, 'update', 'classes', class_id)
    return {'success': True, 'data': serialize(db.classes.find_one(query))}


@router.get('/{class_id}/community')
def class_community(class_id: str, user=Depends(get_current_user)):
    db = database()
    doc = db.classes.find_one({'_id': oid(class_id, 'clase')})
    if not doc:
        fail(404, 'NOT_FOUND', 'Clase no encontrada')
    uid = str(user['_id'])
    if user['role'] == 'student':
        ensure_student_class_scope(db, user, doc)
        if not db.class_memberships.find_one({'class_id': {'$in': id_refs(doc['_id'])},
                                              'student_id': {'$in': id_refs(uid)}}):
            fail(403, 'FORBIDDEN', 'No perteneces a esta clase')
    elif not db.classes.find_one({'$and': [{'_id': doc['_id']}, scope(db, user)]}):
        fail(403, 'FORBIDDEN', 'No puedes consultar esta clase')
    members = db.class_memberships.find({'class_id': {'$in': id_refs(doc['_id'])}}, {'student_id': 1})
    ids = [ObjectId(str(m['student_id'])) for m in members if ObjectId.is_valid(str(m.get('student_id', '')))]
    students = [serialize(x) for x in db.users.find({'_id': {'$in': ids}}, {'full_name': 1, 'avatar': 1, 'banner_color': 1, 'bio': 1})]
    teacher = db.users.find_one({'_id': ObjectId(str(doc['teacher_id']))}, {'full_name': 1, 'avatar': 1, 'banner_color': 1, 'bio': 1}) if ObjectId.is_valid(str(doc.get('teacher_id', ''))) else None
    tutor_ids = assigned_tutor_ids(db, doc, user.get('institution_id'))
    tutors = [serialize(x) for x in db.users.find({
        '_id': {'$in': tutor_ids}, 'is_active': {'$ne': False}, 'is_deleted': {'$ne': True},
    }, {'full_name': 1, 'avatar': 1, 'banner_color': 1, 'bio': 1})]
    if user['role'] == 'student':
        students = [person for person in students if person['id'] != uid]
    return {'success': True, 'data': {'students': students, 'teacher': serialize(teacher) if teacher else None, 'tutors': tutors}}

@router.post('/subjects', status_code=201)
def create_subject_for_class(payload: dict, user=Depends(get_current_user)):
    if user.get('role') not in MANAGERS:
        fail(403, 'FORBIDDEN', 'No puedes crear materias')
    db = database()
    career = managed_career(db, user, payload.get('career_id'))
    name = ' '.join(str(payload.get('name', '')).strip().split())
    if not (2 <= len(name) <= 120):
        fail(422, 'INVALID_NAME', 'Escribe un nombre de materia de 2 a 120 caracteres')
    # Comparación insensible a mayúsculas/minúsculas dentro de la carrera.
    import re
    duplicate = db.subjects.find_one({'career_id': career, 'name': {'$regex': '^' + re.escape(name) + '$', '$options': 'i'}, 'is_deleted': {'$ne': True}})
    if duplicate:
        fail(409, 'SUBJECT_EXISTS', 'La materia ya existe en esta carrera; selecciónala en la lista')
    now = datetime.now(timezone.utc)
    data = {'name': name, 'career_id': career, 'code': str(payload.get('code') or ('MAT-' + secrets.token_hex(3).upper())), 'is_active': True, 'is_deleted': False, 'created_at': now, 'updated_at': now}
    if user.get('institution_id'):
        data['institution_id'] = user['institution_id']
    try:
        result = db.subjects.insert_one(data)
    except DuplicateKeyError:
        fail(409, 'SUBJECT_EXISTS', 'Ya existe una materia con esa clave')
    data['_id'] = result.inserted_id
    audit(db, user, 'create', 'subjects', str(result.inserted_id))
    return {'success': True, 'data': serialize(data)}

@router.get('/{class_id}/students')
def class_students(class_id: str, user=Depends(get_current_user)):
    """Complete class roster, restricted to actual class members and assigned staff."""
    db = database()
    class_oid = oid(class_id, 'clase')
    doc = db.classes.find_one({'_id': class_oid}, {'career_id': 1, 'institution_id': 1})
    if not doc:
        fail(404, 'CLASS_NOT_FOUND', 'Clase no encontrada')
    uid = str(user['_id'])
    # Historical membership documents may contain string or ObjectId identifiers.
    class_refs = [class_oid, str(class_oid)]
    if user['role'] == 'student':
        ensure_student_class_scope(db, user, doc)
        member = db.class_memberships.find_one({
            'class_id': {'$in': class_refs},
            'student_id': {'$in': id_refs(uid)}
        }, {'_id': 1})
        if not member:
            fail(403, 'FORBIDDEN', 'No perteneces a esta clase')
    elif not db.classes.find_one({'$and': [{'_id': class_oid}, scope(db, user)]}, {'_id': 1}):
        fail(403, 'FORBIDDEN', 'No puedes consultar esta clase')
    memberships = db.class_memberships.find({'class_id': {'$in': class_refs}}, {'student_id': 1})
    ids = list({ObjectId(str(m['student_id'])) for m in memberships
                if ObjectId.is_valid(str(m.get('student_id', '')))})
    if not ids:
        return {'success': True, 'data': []}
    students = db.users.find({'_id': {'$in': ids}, 'is_active': {'$ne': False}},
                             {'full_name': 1, 'avatar_path': 1, 'avatar': 1})
    rows = [{'id': str(u['_id']), 'full_name': u.get('full_name') or 'Alumno',
             'avatar_url': f"/api/v1/users/{u['_id']}/avatar" if u.get('avatar_path') or u.get('avatar') else None}
            for u in students]
    rows.sort(key=lambda u: u['full_name'].casefold())
    return {'success': True, 'data': rows}


@router.post('/{class_id}/invite-tutor')
def invite_tutor(class_id: str, payload: dict, user=Depends(get_current_user)):
    """El docente invita a un tutor real de la carrera; no otorga acceso hasta aceptar."""
    db = database()
    doc = db.classes.find_one({'$and': [{'_id': oid(class_id, 'clase')}, scope(db, user)]})
    if not doc or (user['role'] not in {'admin', 'super_admin', 'career_admin'} and str(doc.get('teacher_id')) != str(user['_id'])):
        fail(403, 'FORBIDDEN', 'Solo el docente responsable puede invitar tutores')
    tutor_user_id = str(payload.get('tutor_user_id', ''))
    account = db.users.find_one({'_id': oid(tutor_user_id, 'tutor'), 'role': 'tutor', 'is_active': {'$ne': False}})
    profile = db.tutors.find_one({'user_id': tutor_user_id, 'is_deleted': {'$ne': True}})
    if not account or not profile or str(profile.get('career_id')) != str(doc['career_id']):
        fail(422, 'INVALID_TUTOR', 'Selecciona un tutor activo de la carrera')
    db.class_tutor_invitations.update_one({'class_id': doc['_id'], 'tutor_id': tutor_user_id},
        {'$set': {'status': 'pending', 'invited_at': datetime.now(timezone.utc), 'invited_by': str(user['_id'])}}, upsert=True)
    return {'success': True, 'data': {'message': 'Invitación enviada'}}



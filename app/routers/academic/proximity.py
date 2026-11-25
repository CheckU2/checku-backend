"""Referencia GPS del docente y eventos de permanencia durante la sesión."""
from datetime import datetime, timezone
from uuid import uuid4
from hmac import compare_digest, digest
from secrets import token_hex
from math import asin, cos, isfinite, radians, sin
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit
from app.routers.academic.sessions import student_session_query, teacher_session_query

router = APIRouter(prefix='/proximity', tags=['Proximidad'])
RADIUS_M = 50.0
TEACHER_MAX_ACCURACY_M = 35
GRACE_SECONDS = 120

def dbx():
    db = get_database()
    if db is None: raise HTTPException(503, detail={'message': 'Base de datos no disponible'})
    return db

def session(db, sid):
    if not ObjectId.is_valid(sid): raise HTTPException(422, detail={'message': 'Sesión inválida'})
    doc = db.class_sessions.find_one({'_id': ObjectId(sid)})
    if not doc: raise HTTPException(404, detail={'message': 'Sesión no encontrada'})
    return doc

def teacher_allowed(db, doc, user):
    role = user.get('role')
    if role in ('admin', 'super_admin'): return True
    if role == 'career_admin': return str(doc.get('career_id')) == str(user.get('career_id'))
    return role == 'teacher' and db.class_sessions.find_one({'_id': doc['_id'], **teacher_session_query(db, user)}, {'_id': 1}) is not None

def student_allowed(db, doc, user):
    if user.get('role') != 'student': return False
    return db.class_sessions.find_one({'$and': [{'_id': doc['_id']}, student_session_query(db, user)]}, {'_id': 1}) is not None

def has_attendance(db, sid, user):
    return db.attendance.find_one({'session_id': sid, 'user_id': user['_id'], 'status': {'$in': ['present', 'late']}}) is not None or db.attendance.find_one({'session_id': sid, 'student_id': str(user['_id']), 'status': {'$in': ['present', 'late']}}) is not None

def number(payload, key, lower, upper):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not lower <= value <= upper:
        raise HTTPException(422, detail={'message': f'{key} inválido'})
    return float(value)

def verify_checkin_location(doc, position):
    anchor = doc.get('proximity_anchor')
    if not doc.get('proximity_enabled') or not anchor or doc.get('proximity_radius_m') != RADIUS_M:
        raise HTTPException(409, detail={'message': 'El docente debe generar un QR con ubicación GPS.'})
    if not isinstance(position, dict):
        raise HTTPException(422, detail={'message': 'Se requiere la ubicación del alumno.'})
    distance, uncertainty = distance_from_anchor(anchor, position)
    if position['accuracy_m'] > 25:
        raise HTTPException(422, detail={'message': 'La precisión del GPS es insuficiente.'})
    if distance + uncertainty > RADIUS_M:
        raise HTTPException(403, detail={'message': 'El GPS no pudo confirmar que estás dentro de 50 m del docente.'})


def distance_from_anchor(anchor, position):
    lat = radians(number(position, 'latitude', -90, 90))
    lon = radians(number(position, 'longitude', -180, 180))
    accuracy = number(position, 'accuracy_m', 0, 100000)
    anchor_lat = radians(number(anchor, 'latitude', -90, 90))
    anchor_lon = radians(number(anchor, 'longitude', -180, 180))
    anchor_accuracy = number(anchor, 'accuracy_m', 0, TEACHER_MAX_ACCURACY_M)
    h = sin((lat - anchor_lat) / 2)**2 + cos(lat) * cos(anchor_lat) * sin((lon - anchor_lon) / 2)**2
    distance = 6371000 * 2 * asin(min(1, h**0.5))
    return distance, accuracy + anchor_accuracy


def bluetooth_settings():
    return {
        'bluetooth_checkin_required': True,
        'proximity_ble_uuid': str(uuid4()),
        '_proximity_bluetooth_key': token_hex(32),
        'proximity_enabled': True,
        'gps_checkin_required': False,
    }


def bluetooth_challenge(doc, user, time_slot):
    key = bytes.fromhex(doc['_proximity_bluetooth_key'])
    message = f"checku-challenge:{doc['_id']}:{user['_id']}:{time_slot}".encode()
    return digest(key, message, 'sha256')[:16]


def bluetooth_slot():
    return int(datetime.now(timezone.utc).timestamp()) // 30


def bluetooth_proof_is_valid(doc, user, proof):
    if not isinstance(proof, str) or len(proof) != 32 or not doc.get('_proximity_bluetooth_key'):
        return False
    key = bytes.fromhex(doc['_proximity_bluetooth_key'])
    slot = bluetooth_slot()
    for time_slot in (slot, slot - 1):
        challenge = bluetooth_challenge(doc, user, time_slot)
        expected = digest(key, challenge, 'sha256')[:16].hex()
        if compare_digest(expected.encode(), proof.encode()):
            return True
    return False


@router.get('/sessions/{session_id}/ble/challenge')
def issue_bluetooth_challenge(session_id: str, user=Depends(get_current_user)):
    db = dbx()
    doc = session(db, session_id)
    if not student_allowed(db, doc, user):
        raise HTTPException(403, detail={'message': 'Se requiere inscripción en esta clase.'})
    if doc.get('status') != 'active' or not doc.get('bluetooth_checkin_required') or not doc.get('proximity_enabled'):
        raise HTTPException(409, detail={'message': 'La sesión Bluetooth no está activa.'})
    return {'success': True, 'data': {
        'service': doc['proximity_ble_uuid'],
        'challenge': bluetooth_challenge(doc, user, bluetooth_slot()).hex(),
    }}


def gps_settings(position, now):
    if not isinstance(position, dict):
        raise HTTPException(422, detail={'message': 'Ubicación del docente inválida.'})
    anchor = {
        'latitude': number(position, 'latitude', -90, 90),
        'longitude': number(position, 'longitude', -180, 180),
        'accuracy_m': number(position, 'accuracy_m', 0, TEACHER_MAX_ACCURACY_M),
        'recorded_at': now,
        'source': 'teacher_phone_gps',
    }
    return {
        'proximity_anchor': anchor, 'proximity_enabled': True,
        'proximity_radius_m': RADIUS_M, 'proximity_grace_seconds': GRACE_SECONDS,
        'gps_checkin_required': True,
    }


@router.post('/sessions/{session_id}/anchor')
def set_anchor(session_id: str, payload: dict, user=Depends(get_current_user)):
    db = dbx(); doc = session(db, session_id)
    if not teacher_allowed(db, doc, user): raise HTTPException(403, detail={'message': 'Solo el docente responsable puede publicar el punto de referencia'})
    if doc.get('status') != 'active': raise HTTPException(409, detail={'message': 'La sesión no está en curso'})
    settings = gps_settings(payload, datetime.now(timezone.utc))
    anchor = settings['proximity_anchor']
    db.class_sessions.update_one({'_id': doc['_id']}, {'$set': settings})
    audit(db, user, 'proximity_anchor', 'class_sessions', session_id)
    return {'success': True, 'data': {'enabled': True, 'anchor': serialize(anchor), 'radius_m': RADIUS_M, 'grace_seconds': GRACE_SECONDS}}

@router.get('/sessions/{session_id}/reference')
def reference(session_id: str, user=Depends(get_current_user)):
    db = dbx(); doc = session(db, session_id)
    if not (teacher_allowed(db, doc, user) or student_allowed(db, doc, user)):
        raise HTTPException(403, detail={'message': 'Sin acceso a esta sesión'})
    if doc.get('status') != 'active': raise HTTPException(409, detail={'message': 'La sesión ya no está activa'})
    if user.get('role') == 'student' and not has_attendance(db, session_id, user):
        raise HTTPException(403, detail={'message': 'Registra primero tu asistencia'})
    anchor = doc.get('proximity_anchor')
    return {'success': True, 'data': {'enabled': bool(anchor and doc.get('proximity_enabled')), 'anchor': serialize(anchor) if anchor and user.get('role') != 'student' else None, 'radius_m': RADIUS_M, 'grace_seconds': GRACE_SECONDS}}

@router.post('/sessions/{session_id}/events')
def event(session_id: str, payload: dict, user=Depends(get_current_user)):
    db = dbx(); doc = session(db, session_id)
    if not student_allowed(db, doc, user) or not has_attendance(db, session_id, user):
        raise HTTPException(403, detail={'message': 'Solo alumnos inscritos con asistencia registrada'})
    if doc.get('status') != 'active' or not doc.get('proximity_enabled') or not (doc.get('proximity_anchor') or doc.get('proximity_ble_uuid')):
        raise HTTPException(409, detail={'message': 'Proximidad no disponible'})
    kind = payload.get('kind')
    if doc.get('bluetooth_checkin_required'):
        kind = 'inside' if bluetooth_proof_is_valid(doc, user, payload.get('bluetooth_proof')) else 'uncertain'
    elif doc.get('gps_checkin_required'):
        position = payload.get('location')
        if isinstance(position, dict):
            distance, uncertainty = distance_from_anchor(doc['proximity_anchor'], position)
            if uncertainty <= RADIUS_M and distance - uncertainty > RADIUS_M:
                kind = 'outside'
            elif distance + uncertainty <= RADIUS_M:
                kind = 'inside'
            else:
                kind = 'uncertain'
        elif kind != 'uncertain':
            raise HTTPException(422, detail={'message': 'Se requiere una lectura de GPS.'})
    if kind not in ('inside', 'outside', 'uncertain'):
        raise HTTPException(422, detail={'message': 'Estado inválido'})
    now = datetime.now(timezone.utc)
    key = {'session_id': session_id, 'student_id': str(user['_id'])}
    previous = db.proximity_states.find_one(key) or {}
    outside_since = previous.get('outside_since')
    if outside_since is not None and outside_since.tzinfo is None:
        outside_since = outside_since.replace(tzinfo=timezone.utc)
    if kind == 'inside':
        outside_since = None
        state = 'present'
    elif kind == 'uncertain':
        # No acumular tiempo cuando la señal es incierta.
        outside_since = None
        state = 'uncertain'
    else:
        if outside_since is None: outside_since = now
        state = 'possible_abandonment' if (now - outside_since).total_seconds() >= GRACE_SECONDS else 'observing'
    doc_state = {'state': state, 'last_kind': kind, 'outside_since': outside_since, 'updated_at': now, 'requires_review': state == 'possible_abandonment'}
    changed = state != previous.get('state') or (outside_since is None) != (previous.get('outside_since') is None)
    previous_update = previous.get('updated_at')
    if previous_update is not None and previous_update.tzinfo is None:
        previous_update = previous_update.replace(tzinfo=timezone.utc)
    stale = previous_update is None or (now - previous_update).total_seconds() >= 60
    # Una escritura por cambio de estado; en estado estable, como máximo una por minuto.
    if changed or stale:
        db.proximity_states.update_one(key, {'$set': doc_state}, upsert=True)
    if changed:
        db.proximity_events.insert_one({**key, **doc_state, 'source': 'ble_android_experimental' if doc.get('proximity_ble_uuid') else 'gps_experimental', 'created_at': now})
    return {'success': True, 'data': {'state': state, 'outside_since': outside_since.isoformat() if outside_since else None, 'remaining_seconds': max(0, GRACE_SECONDS - int((now - outside_since).total_seconds())) if outside_since else 0, 'requires_review': state == 'possible_abandonment'}}

@router.get('/sessions/{session_id}/events')
def events(session_id: str, user=Depends(get_current_user)):
    db = dbx(); doc = session(db, session_id)
    if not teacher_allowed(db, doc, user): raise HTTPException(403, detail={'message': 'Sin permisos'})
    rows = list(db.proximity_states.find({'session_id': session_id}).limit(300))
    ids = [ObjectId(r['student_id']) for r in rows if ObjectId.is_valid(r['student_id'])]
    names = {str(x['_id']): x.get('full_name', 'Alumno') for x in db.users.find({'_id': {'$in': ids}}, {'full_name': 1})}
    for r in rows: r['full_name'] = names.get(r['student_id'], 'Alumno')
    return {'success': True, 'data': [serialize(x) for x in rows]}

@router.post('/sessions/{session_id}/deactivate')
def deactivate(session_id: str, user=Depends(get_current_user)):
    db = dbx(); doc = session(db, session_id)
    if not teacher_allowed(db, doc, user): raise HTTPException(403, detail={'message': 'Sin permisos'})
    db.class_sessions.update_one({'_id': doc['_id']}, {'$set': {'proximity_enabled': False}})
    return {'success': True, 'data': {'enabled': False}}


@router.post('/sessions/{session_id}/ble/start')
def ble_start(session_id: str, user=Depends(get_current_user)):
    """Activate an opaque per-session BLE service UUID. One DB update only per activation."""
    db = dbx(); doc = session(db, session_id)
    # Do not permit administrators or tutors to impersonate the session teacher's phone.
    if user.get('role') != 'teacher' or str(doc.get('teacher_id')) != str(user['_id']):
        raise HTTPException(403, detail={'message': 'Solo el docente de la sesión puede activar el emisor BLE'})
    if doc.get('status') != 'active': raise HTTPException(409, detail={'message': 'La sesión no está activa'})
    token = doc.get('proximity_ble_uuid')
    if not token:
        token = str(uuid4())
        db.class_sessions.update_one({'_id': doc['_id'], 'status': 'active'},
            {'$set': {'proximity_ble_uuid': token, 'proximity_enabled': True, 'proximity_grace_seconds': GRACE_SECONDS}})
    elif not doc.get('proximity_enabled'):
        db.class_sessions.update_one({'_id': doc['_id']}, {'$set': {'proximity_enabled': True}})
    return {'success': True, 'data': {'service_uuid': token, 'grace_seconds': GRACE_SECONDS}}

@router.get('/sessions/{session_id}/ble/config')
def ble_config(session_id: str, user=Depends(get_current_user)):
    db = dbx(); doc = session(db, session_id)
    if not student_allowed(db, doc, user) or not has_attendance(db, session_id, user):
        raise HTTPException(403, detail={'message': 'Se requiere inscripción y asistencia registrada'})
    if doc.get('status') != 'active' or not doc.get('proximity_enabled') or not doc.get('proximity_ble_uuid'):
        raise HTTPException(409, detail={'message': 'El docente aún no ha activado Bluetooth'})
    return {'success': True, 'data': {'service_uuid': doc['proximity_ble_uuid'], 'grace_seconds': GRACE_SECONDS}}

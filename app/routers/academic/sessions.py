from datetime import datetime, timedelta, timezone
import secrets
import json
import base64
import io
import qrcode
import qrcode.image.svg
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from app.config import get_settings
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit

router=APIRouter(prefix="/sessions",tags=["Sesiones de clase y QR"])
def qr_image(session_id, token):
    payload = json.dumps({'session_id': session_id, 'qr_token': token}, separators=(',', ':'))
    qr = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, box_size=9, border=3)
    buffer = io.BytesIO()
    qr.save(buffer)
    return 'data:image/svg+xml;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')

def db_or_503():
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    return db
def allowed_teacher(db,user,session):
    role=user.get("role")
    if role in {"super_admin","admin"}: return True
    if role=="career_admin": return session.get("career_id")==user.get("career_id")
    return session.get("teacher_id")==str(user["_id"]) or session.get("teacher_id")==user["_id"]
def details(db, doc):
    row = serialize(doc)
    for field, collection, name in (("subject_id", "subjects", "subject_name"), ("group_id", "groups", "group_name"), ("period_id", "academic_periods", "period_name")):
        value = str(doc.get(field, ""))
        ref = db[collection].find_one({"_id": ObjectId(value)}) if ObjectId.is_valid(value) else None
        row[name] = (ref or {}).get("name") or (ref or {}).get("code") or "Sin asignar"
    return row

def student_class_ids(db, user):
    return [str(x["class_id"]) for x in db.class_memberships.find({"student_id":str(user["_id"])}, {"class_id":1})]

def student_session_query(db, user):
    ids = student_class_ids(db, user)
    return {"class_id": {"$in": ids}} if ids else {"_id": {"$in": []}}

def tutor_session_query(db, user):
    from app.routers.academic.classes import scope
    classes = db.classes.find(scope(db, user), {"_id":1})
    ids = [str(c["_id"]) for c in classes]
    return {"class_id": {"$in": ids}} if ids else {"_id": {"$in": []}}

@router.post("",status_code=201)
def create_session(payload:dict,user=Depends(get_current_user)):
    if user.get("role") not in {"teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
    db=db_or_503(); now=datetime.now(timezone.utc)
    data={k:payload[k] for k in ("subject_id","group_id","period_id","career_id","schedule_id","starts_at") if k in payload}
    class_id = str(payload.get('class_id', ''))
    linked_class = db.classes.find_one({'_id': ObjectId(class_id), 'is_active': {'$ne': False}}) if ObjectId.is_valid(class_id) else None
    if linked_class:
        if user.get('role') == 'teacher' and str(linked_class['teacher_id']) != str(user['_id']):
            raise HTTPException(403, detail={'message': 'Esta clase pertenece a otro docente'})
        if user.get('role') == 'career_admin' and str(linked_class['career_id']) != str(user.get('career_id')):
            raise HTTPException(403, detail={'message': 'Clase de otra carrera'})
        data.update({k: linked_class[k] for k in ('subject_id', 'group_id', 'period_id', 'career_id')})
        data['class_id'] = class_id
    elif class_id:
        raise HTTPException(422, detail={'message': 'Selecciona una clase válida'})
    if user.get("role")=="teacher": data["teacher_id"]=str(user["_id"])
    elif "teacher_id" in payload and user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"No puedes asignar otro docente"})
    else: data["teacher_id"]=str(linked_class["teacher_id"] if linked_class else payload.get("teacher_id",user["_id"]))
    if user.get("role")=="career_admin": data["career_id"]=user.get("career_id")
    if user.get("role")=="teacher":
        required={"subject_id","group_id","period_id"}
        if not required.issubset(data): raise HTTPException(422,detail={"code":"MISSING_ASSIGNMENT","message":"subject_id, group_id y period_id son obligatorios"})
        assignment=db.teaching_assignments.find_one({"teacher_id":str(user["_id"]),"subject_id":data["subject_id"],"group_id":data["group_id"],"period_id":data["period_id"],"is_active":{"$ne":False}})
        if not assignment and not (linked_class and str(linked_class.get('teacher_id')) == str(user['_id'])):
            raise HTTPException(403,detail={"code":"NOT_ASSIGNED","message":"No tienes asignada esta materia y grupo"})
    for key in ("subject_id","group_id","period_id","schedule_id"):
        if key in data and not ObjectId.is_valid(str(data[key])): raise HTTPException(422,detail={"code":"INVALID_ID","message":f"{key} inválido"})
    data.update({"status":"scheduled","created_at":now,"updated_at":now})
    res=db.class_sessions.insert_one(data); data["_id"]=res.inserted_id
    audit(db,user,"create","class_sessions",str(res.inserted_id))
    return {"success":True,"data":serialize(data)}
@router.get("")
def list_sessions(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),user=Depends(get_current_user)):
    db=db_or_503(); q={}
    if user.get("role")=="teacher": q["teacher_id"]=str(user["_id"])
    elif user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
    elif user.get("role")=="student": q.update(student_session_query(db, user))
    elif user.get("role")=="tutor": q.update(tutor_session_query(db, user))
    elif user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
    total=db.class_sessions.count_documents(q)
    docs=db.class_sessions.find(q).sort("created_at",-1).skip((page-1)*limit).limit(limit)
    return {"success":True,"data":[details(db,x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
@router.get("/active")
def active(user=Depends(get_current_user)):
    db=db_or_503(); q={"status":"active"}
    if user.get("role")=="teacher": q["teacher_id"]=str(user["_id"])
    elif user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
    elif user.get("role")=="student":
        q.update(student_session_query(db, user))
    elif user.get("role")=="tutor": q.update(tutor_session_query(db, user))
    elif user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
    return {"success":True,"data":[details(db,x) for x in db.class_sessions.find(q).sort("started_at",-1).limit(100)]}
@router.get("/{session_id}")
def get_session(session_id:str,user=Depends(get_current_user)):
    if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
    db=db_or_503(); doc=db.class_sessions.find_one({"_id":ObjectId(session_id)})
    if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
    if user.get("role")=="tutor" and str(doc.get("class_id")) not in tutor_session_query(db,user).get("class_id",{}).get("$in",[]): raise HTTPException(403,detail={"message":"No tienes asignada esta sesión"})
    if not allowed_teacher(db,user,doc) and user.get("role") not in {"student","tutor"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
    if user.get("role")=="student" and str(doc.get("class_id")) not in student_class_ids(db, user): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
    return {"success":True,"data":serialize(doc)}
@router.get('/{session_id}/report')
def session_report(session_id: str, user=Depends(get_current_user)):
    db = db_or_503()
    if not ObjectId.is_valid(session_id):
        raise HTTPException(422, detail={'message': 'Sesión inválida'})
    session = db.class_sessions.find_one({'_id': ObjectId(session_id)})
    if not session:
        raise HTTPException(404, detail={'message': 'Sesión no encontrada'})
    role = user.get('role')
    if role == 'student':
        raise HTTPException(403, detail={'message': 'Solo el personal autorizado puede ver el reporte'})
    if role == 'tutor':
        permitted = tutor_session_query(db, user).get('class_id', {}).get('$in', [])
        if str(session.get('class_id')) not in permitted:
            raise HTTPException(403, detail={'message': 'No tienes acceso a esta clase'})
    elif not allowed_teacher(db, user, session):
        raise HTTPException(403, detail={'message': 'No tienes acceso a esta sesión'})
    cid = str(session.get('class_id', ''))
    memberships = list(db.class_memberships.find({'class_id': ObjectId(cid)}, {'student_id': 1, 'joined_at': 1})) if ObjectId.is_valid(cid) else []
    # Para sesiones históricas, solo alumnos inscritos al momento de finalizar.
    ended = session.get('ended_at')
    if ended:
        memberships = [m for m in memberships if not m.get('joined_at') or m['joined_at'] <= ended]
    uids = [str(m['student_id']) for m in memberships]
    valid = [ObjectId(x) for x in uids if ObjectId.is_valid(x)]
    users = {str(u['_id']): u for u in db.users.find({'_id': {'$in': valid}}, {'full_name': 1, 'avatar_url': 1, 'profile_photo': 1})}
    attendance = {str(a.get('student_id')): a for a in db.attendance.find({'session_id': session_id})}
    entries = []
    for uid in uids:
        a = attendance.get(uid)
        profile = users.get(uid, {})
        entries.append({'student_id': uid, 'full_name': profile.get('full_name', 'Alumno'), 'avatar_url': profile.get('avatar_url') or profile.get('profile_photo'), 'status': a.get('status', 'absent' if session.get('status') == 'closed' else 'pending') if a else ('absent' if session.get('status') == 'closed' else 'pending'), 'recorded_at': a.get('recorded_at') if a else None})
    counts = {key: sum(x['status'] == key for x in entries) for key in ('present','late','absent','excused','pending')}
    return {'success': True, 'data': {'session': details(db, session), 'students': entries, 'counts': counts, 'departure_tracking': False}}

@router.post("/{session_id}/start")
def start(session_id:str,user=Depends(get_current_user)):
    if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
    db=db_or_503(); now=datetime.now(timezone.utc); doc=db.class_sessions.find_one({"_id":ObjectId(session_id)})
    if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
    if not allowed_teacher(db,user,doc): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"No puedes iniciar esta clase"})
    if user.get("role")=="teacher":
        assigned=db.teaching_assignments.find_one({"teacher_id":str(user["_id"]),"subject_id":doc.get("subject_id"),"group_id":doc.get("group_id"),"period_id":doc.get("period_id"),"is_active":{"$ne":False}})
        linked = db.classes.find_one({'_id': ObjectId(doc['class_id']), 'teacher_id': str(user['_id'])}) if ObjectId.is_valid(str(doc.get('class_id', ''))) else None
        if not assigned and not linked: raise HTTPException(403,detail={"code":"NOT_ASSIGNED","message":"Ya no tienes asignada esta clase"})
    if doc.get("status")=="closed": raise HTTPException(409,detail={"code":"SESSION_CLOSED","message":"La clase ya está cerrada"})
    db.class_sessions.update_one({"_id":doc["_id"],"status":{"$ne":"closed"}},{"$set":{"status":"active","started_at":doc.get("started_at",now),"updated_at":now}})
    fresh=db.class_sessions.find_one({"_id":doc["_id"]})
    if not db.qr_challenges.find_one({"session_id":str(doc["_id"]),"revoked_at":None,"expires_at":{"$gt":now}}):
        raw=secrets.token_urlsafe(32); code=f"{secrets.randbelow(10000):04d}"
        while db.qr_challenges.find_one({"code_hash":__import__("hashlib").sha256(code.encode()).hexdigest(),"revoked_at":None,"expires_at":{"$gt":now}}): code=f"{secrets.randbelow(10000):04d}"
        db.qr_challenges.insert_one({"session_id":str(doc["_id"]),"token_hash":__import__("hashlib").sha256(raw.encode()).hexdigest(),"code_hash":__import__("hashlib").sha256(code.encode()).hexdigest(),"code_last4":code[-2:],"created_at":now,"expires_at":now+timedelta(seconds=get_settings().qr_challenge_ttl_seconds),"revoked_at":None,"attempts":0})
        fresh["qr"]={"token":raw,"code":code,"expires_at":now+timedelta(seconds=get_settings().qr_challenge_ttl_seconds),"image":qr_image(str(doc["_id"]), raw)}
    audit(db,user,"start","class_sessions",session_id)
    return {"success":True,"data":serialize(fresh)}
@router.post("/{session_id}/end")
def end(session_id:str,user=Depends(get_current_user)):
    if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
    db=db_or_503(); now=datetime.now(timezone.utc); doc=db.class_sessions.find_one({"_id":ObjectId(session_id)})
    if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
    if not allowed_teacher(db,user,doc): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"No puedes cerrar esta clase"})
    if doc.get("status")!="closed":
        db.class_sessions.update_one({"_id":doc["_id"],"status":{"$ne":"closed"}},{"$set":{"status":"closed","ended_at":now,"updated_at":now}})
        db.qr_challenges.update_many({"session_id":session_id,"revoked_at":None},{"$set":{"revoked_at":now}})
        # Absences are inserted only for enrolled students without an attendance record.
        members = db.class_memberships.find({"class_id":ObjectId(doc["class_id"])}) if ObjectId.is_valid(str(doc.get("class_id", ""))) else []
        for e in members:
            sid=str(e.get("student_id"))
            db.attendance.update_one({"session_id":session_id,"student_id":sid},{"$setOnInsert":{"session_id":session_id,"student_id":sid,"status":"absent","recorded_at":now,"created_by":"system"}},upsert=True)
        audit(db,user,"end","class_sessions",session_id)
    return {"success":True,"data":serialize(db.class_sessions.find_one({"_id":doc["_id"]}))}
@router.post("/{session_id}/qr/rotate")
def rotate_qr(session_id:str,user=Depends(get_current_user)):
    if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
    db=db_or_503(); doc=db.class_sessions.find_one({"_id":ObjectId(session_id)})
    if not doc or not allowed_teacher(db,user,doc): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
    if doc.get("status")!="active": raise HTTPException(409,detail={"code":"SESSION_NOT_ACTIVE","message":"La clase no está activa"})
    now=datetime.now(timezone.utc); db.qr_challenges.update_many({"session_id":session_id,"revoked_at":None},{"$set":{"revoked_at":now}})
    raw=secrets.token_urlsafe(32); code=f"{secrets.randbelow(10000):04d}"
    import hashlib
    while db.qr_challenges.find_one({"code_hash":hashlib.sha256(code.encode()).hexdigest(),"revoked_at":None,"expires_at":{"$gt":now}}): code=f"{secrets.randbelow(10000):04d}"
    db.qr_challenges.insert_one({"session_id":session_id,"token_hash":hashlib.sha256(raw.encode()).hexdigest(),"code_hash":hashlib.sha256(code.encode()).hexdigest(),"created_at":now,"expires_at":now+timedelta(seconds=get_settings().qr_challenge_ttl_seconds),"revoked_at":None,"attempts":0})
    return {"success":True,"data":{"token":raw,"code":code,"expires_at":now+timedelta(seconds=get_settings().qr_challenge_ttl_seconds),"image":qr_image(str(doc["_id"]), raw)}}
@router.post("/{session_id}/qr/revoke")
def revoke_qr(session_id:str,user=Depends(get_current_user)):
    if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
    db=db_or_503(); doc=db.class_sessions.find_one({"_id":ObjectId(session_id)})
    if not doc or not allowed_teacher(db,user,doc): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
    db.qr_challenges.update_many({"session_id":session_id,"revoked_at":None},{"$set":{"revoked_at":datetime.now(timezone.utc)}})
    return {"success":True,"data":{"revoked":True}}
@router.get("/{session_id}/status")
def status(session_id:str,user=Depends(get_current_user)): return get_session(session_id,user)

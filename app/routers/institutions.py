"""Safe opt-in institution administration, independent of legacy academic CRUD."""
import re
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import EmailStr, TypeAdapter, ValidationError
from pymongo.errors import DuplicateKeyError
from app.database import get_database
from app.dependencies import get_current_user
from app.services.institutions import institution, public_institution, check_platform, is_institution_admin, now_utc
from app.services.authorization import canonical_role
from app.services.users import create_user, public_user

router=APIRouter(tags=["Instituciones"])
def db_required():
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    return db

@router.get('/institutions/public')
def list_public():
    db=db_required()
    return {"success":True,"data":[public_institution(x) for x in db.institutions.find({"status":"active"}).sort("name",1).limit(100)]}

@router.get('/institutions/{institution_id}/careers/public')
def public_careers(institution_id:str):
    db=db_required(); institution(db,institution_id)
    return {"success":True,"data":[{"id":str(x['_id']),"name":x.get('name',''),"code":x.get('code')} for x in db.careers.find({"institution_id":institution_id,"is_deleted":{"$ne":True}}).limit(200)]}

@router.post('/platform/institutions',status_code=201)
def create_institution(payload:dict,user=Depends(get_current_user)):
    check_platform(user); db=db_required()
    name=payload.get('name'); slug=payload.get('slug'); period=payload.get('period_type','quarter')
    if not isinstance(name,str) or not 3<=len(name.strip())<=150 or not isinstance(slug,str) or not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*',slug) or len(slug)>80:
        raise HTTPException(422,detail={'code':'INVALID_INSTITUTION','message':'Nombre o identificador inválido'})
    if period not in ('quarter','semester'): raise HTTPException(422,detail={'code':'INVALID_PERIOD','message':'Periodo inválido'})
    doc={'name':name.strip(),'slug':slug,'period_type':period,'status':'active','settings':{},'created_at':now_utc(),'updated_at':now_utc()}
    try: result=db.institutions.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,detail={'code':'SLUG_EXISTS','message':'Identificador ocupado'})
    return {'success':True,'data':{'id':str(result.inserted_id),'status':'active'},'message':'Institución creada y activada correctamente'}

def audit_institution_action(db,user,institution_id,action,entity,entity_id,details=None):
    db.audit_logs.insert_one({
        'actor_id':user['_id'],
        'actor_role':user.get('role'),
        'institution_id':institution_id,
        'action':action,
        'entity':entity,
        'entity_id':entity_id,
        'details':details or {},
        'created_at':now_utc(),
    })

def institution_career(db,institution_id,career_id):
    if not isinstance(career_id,str) or not ObjectId.is_valid(career_id):
        return None
    return db.careers.find_one({
        '_id':ObjectId(career_id),
        'institution_id':institution_id,
        'is_deleted':{'$ne':True},
    })

def can_review_requests(db,user,institution_id,request_doc=None):
    if is_institution_admin(db,user,institution_id):
        return True
    director = career_director_assignment(db, user, institution_id)
    if not director:
        return False
    return request_doc is None or (
        request_doc.get('scope_type')=='career'
        and str(request_doc.get('scope_id'))==str(director.get('scope_id'))
    )


def career_director_assignment(db, user, institution_id):
    if user.get('role') != 'career_admin' or (
        user.get('institution_id') and user['institution_id'] != institution_id
    ):
        return None
    query = {
        'institution_id':institution_id,
        'user_id':str(user['_id']),
        'role':{'$in':['career_director','career_admin']},
        'scope_type':'career',
        'status':'active',
    }
    if user.get('active_context'):
        query['scope_id'] = user['active_context']['scope_id']
    return db.role_assignments.find_one(query)

@router.get('/institutions/{institution_id}/members')
def list_institution_members(institution_id:str,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id)
    director = career_director_assignment(db, user, institution_id)
    if not director and not is_institution_admin(db,user,institution_id):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para consultar miembros'})
    memberships=list(db.institution_memberships.find({'institution_id':institution_id,'status':{'$in':['active','pending']}}).limit(500))
    user_ids=[ObjectId(str(member['user_id'])) for member in memberships if ObjectId.is_valid(str(member.get('user_id','')))]
    users={str(item['_id']):item for item in db.users.find({'_id':{'$in':user_ids}},{'password_hash':0})}
    assignments=list(db.role_assignments.find({'institution_id':institution_id,'status':'active'}))
    career_id = str(director['scope_id']) if director else None
    if career_id:
        assignments = [item for item in assignments
                       if str(item.get('career_id') or (item.get('scope_id') if item.get('scope_type') == 'career' else '')) == career_id]
    roles_by_user={}
    for assignment in assignments:
        roles_by_user.setdefault(str(assignment.get('user_id')),[]).append({
            'role':canonical_role(assignment.get('role')),
            'scope_type':assignment.get('scope_type'),
            'scope_id':assignment.get('scope_id'),
        })
    return {'success':True,'data':[
        {
            'id':user_id,
            'full_name':users[user_id].get('full_name',''),
            'email':users[user_id].get('email',''),
            'account_type':users[user_id].get('account_type',users[user_id].get('role')),
            'career_id':users[user_id].get('career_id'),
            'membership_status':membership.get('status'),
            'roles':roles_by_user.get(user_id,[]),
        }
        for membership in memberships
        if (user_id:=str(membership.get('user_id'))) in users
        and (not career_id or user_id in roles_by_user or str(users[user_id].get('career_id')) == career_id)
    ]}

@router.get('/institutions/{institution_id}/scopes')
def list_institution_scopes(institution_id:str,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id)
    director = career_director_assignment(db, user, institution_id)
    if not director and not is_institution_admin(db,user,institution_id):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para consultar alcances'})
    career_query = {'institution_id':institution_id,'is_deleted':{'$ne':True}}
    if director:
        career_query['_id'] = ObjectId(director['scope_id'])
    careers=list(db.careers.find(career_query,{'name':1,'code':1}).limit(300))
    career_ids=[ref for item in careers for ref in (str(item['_id']), item['_id'])]
    academic_scope={'career_id':{'$in':career_ids},'institution_id':{'$in':[institution_id,None]},'is_deleted':{'$ne':True}}
    groups=list(db.groups.find(academic_scope,{'name':1,'code':1,'career_id':1}).limit(500)) if career_ids else []
    classes=list(db.classes.find(academic_scope,{'subject_id':1,'career_id':1,'group_id':1}).limit(500)) if career_ids else []
    return {'success':True,'data':{
        'careers':[{'id':str(item['_id']),'name':item.get('name',''),'code':item.get('code')} for item in careers],
        'groups':[{'id':str(item['_id']),'name':item.get('name',''),'code':item.get('code'),'career_id':str(item['career_id'])} for item in groups],
        'classes':[{'id':str(item['_id']),'career_id':str(item['career_id']),'group_id':str(item['group_id']) if item.get('group_id') else None} for item in classes],
    }}

@router.post('/platform/institutions/{institution_id}/owner',status_code=201)
def assign_institution_owner(institution_id:str,payload:dict,user=Depends(get_current_user)):
    check_platform(user); db=db_required(); institution(db,institution_id,active=False)
    try:
        email=str(TypeAdapter(EmailStr).validate_python(payload.get('email',''))).lower().strip()
    except ValidationError:
        raise HTTPException(422,detail={'code':'INVALID_EMAIL','message':'Correo electrónico inválido'})
    assignment_query={
        'institution_id':institution_id,
        'role':'institution_owner',
        'scope_type':'institution',
        'scope_id':None,
    }
    existing_owner=db.role_assignments.find_one({**assignment_query,'status':'active'})
    account=db.users.find_one({'email':email,'is_active':True})
    if existing_owner and (account is None or existing_owner.get('user_id')!=str(account['_id'])):
        raise HTTPException(409,detail={'code':'OWNER_EXISTS','message':'La institución ya tiene propietario. Solicita una transferencia auditada.'})
    if account is None:
        name=payload.get('full_name'); password=payload.get('password')
        if not isinstance(name,str) or not 2<=len(name.strip())<=120:
            raise HTTPException(422,detail={'code':'INVALID_NAME','message':'Escribe el nombre del propietario'})
        if not isinstance(password,str) or not 12<=len(password)<=128:
            raise HTTPException(422,detail={'code':'INVALID_PASSWORD','message':'La contraseña debe tener entre 12 y 128 caracteres'})
        try:
            account=create_user(email=email,password=password,role='student',full_name=name.strip())
        except ValueError as exc:
            raise HTTPException(409,detail={'code':'EMAIL_EXISTS','message':str(exc)})
        db.users.update_one({'_id':account['_id']},{'$set':{'account_type':'staff'}})
        account['account_type']='staff'
    now=now_utc()
    db.institution_memberships.update_one(
        {'institution_id':institution_id,'user_id':str(account['_id'])},
        {'$set':{'status':'active','updated_at':now},'$setOnInsert':{'joined_at':now}},
        upsert=True,
    )
    db.role_assignments.update_one(
        {**assignment_query,'user_id':str(account['_id'])},
        {'$set':{'status':'active','granted_by':str(user['_id']),'granted_at':now}},
        upsert=True,
    )
    audit_institution_action(db,user,institution_id,'assign_owner','users',str(account['_id']))
    return {'success':True,'data':public_user(account),'message':'Propietario institucional asignado'}

@router.get('/institutions/{institution_id}/role-requests')
def list_role_requests(institution_id:str,status:str='pending',user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id)
    if status not in ('pending','approved','rejected'):
        raise HTTPException(422,detail={'code':'INVALID_STATUS','message':'Estado de solicitud inválido'})
    if not can_review_requests(db,user,institution_id):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para revisar solicitudes'})
    query={'institution_id':institution_id,'status':status}
    if user.get('role')=='career_admin' and not is_institution_admin(db,user,institution_id):
        director=career_director_assignment(db, user, institution_id)
        query.update({'scope_type':'career','scope_id':director['scope_id']})
    requests=list(db.role_requests.find(query).sort('created_at',-1).limit(200))
    result=[]
    for request in requests:
        target=db.users.find_one({'_id':ObjectId(request['user_id'])},{'full_name':1,'email':1}) if ObjectId.is_valid(str(request.get('user_id',''))) else None
        result.append({
            'id':str(request['_id']),
            'user_id':request.get('user_id'),
            'full_name':(target or {}).get('full_name',''),
            'email':(target or {}).get('email',''),
            'institution_id':institution_id,
            'requested_role':request.get('requested_role'),
            'scope_type':request.get('scope_type'),
            'scope_id':request.get('scope_id'),
            'status':request.get('status'),
            'created_at':request.get('created_at'),
            'reason':request.get('reason'),
        })
    return {'success':True,'data':result}

@router.post('/institutions/{institution_id}/role-requests/{request_id}/approve')
def approve_role_request(institution_id:str,request_id:str,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id)
    if not ObjectId.is_valid(request_id):
        raise HTTPException(422,detail={'code':'INVALID_ID','message':'Solicitud inválida'})
    request=db.role_requests.find_one({'_id':ObjectId(request_id),'institution_id':institution_id,'status':'pending'})
    if not request:
        raise HTTPException(404,detail={'code':'REQUEST_NOT_FOUND','message':'Solicitud pendiente no encontrada'})
    if not can_review_requests(db,user,institution_id,request):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para aprobar esta solicitud'})
    if request.get('requested_role')!='teacher':
        raise HTTPException(409,detail={'code':'UNSUPPORTED_REQUEST','message':'Tipo de solicitud no compatible'})
    target_id=str(request.get('user_id',''))
    if not ObjectId.is_valid(target_id):
        raise HTTPException(409,detail={'code':'INVALID_REQUEST_USER','message':'La cuenta solicitante no es válida'})
    target=db.users.find_one({'_id':ObjectId(target_id),'is_active':True})
    if not target:
        raise HTTPException(409,detail={'code':'USER_UNAVAILABLE','message':'La cuenta solicitante no está activa'})
    career_id=request.get('scope_id') if request.get('scope_type')=='career' else None
    if career_id and not institution_career(db,institution_id,career_id):
        raise HTTPException(409,detail={'code':'INVALID_CAREER','message':'La carrera solicitada ya no pertenece a la institución'})
    now=now_utc()
    updated=db.role_requests.update_one(
        {'_id':request['_id'],'institution_id':institution_id,'status':'pending'},
        {'$set':{'status':'approved','reviewed_by':str(user['_id']),'reviewed_at':now,'reason':None}},
    )
    if updated.modified_count!=1:
        raise HTTPException(409,detail={'code':'REQUEST_ALREADY_REVIEWED','message':'La solicitud ya fue revisada'})
    db.institution_memberships.update_one(
        {'institution_id':institution_id,'user_id':target_id},
        {'$set':{'status':'active','updated_at':now},'$setOnInsert':{'joined_at':now}},
        upsert=True,
    )
    assignment={
        'institution_id':institution_id,'user_id':target_id,'role':'teacher',
        'scope_type':'career' if career_id else 'institution','scope_id':career_id,
    }
    if career_id:
        assignment['career_id']=career_id
    assignment_key={key:value for key,value in assignment.items() if key!='career_id'}
    assignment_changes={'status':'active','granted_by':str(user['_id']),'granted_at':now,'source_request_id':str(request['_id'])}
    if career_id:
        assignment_changes['career_id']=career_id
    db.role_assignments.update_one(
        assignment_key,
        {'$set':assignment_changes},
        upsert=True,
    )
    upsert_staff_profile(db.teachers, institution_id, career_id, target_id, now)
    db.users.update_one({'_id':target['_id']},{'$set':{'account_type':'teacher','updated_at':now}})
    audit_institution_action(db,user,institution_id,'approve','role_requests',str(request['_id']),{'requested_role':'teacher','user_id':target_id})
    return {'success':True,'data':{'id':str(request['_id']),'status':'approved','user_id':target_id}}

@router.post('/institutions/{institution_id}/role-requests/{request_id}/reject')
def reject_role_request(institution_id:str,request_id:str,payload:dict,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id)
    if not ObjectId.is_valid(request_id):
        raise HTTPException(422,detail={'code':'INVALID_ID','message':'Solicitud inválida'})
    request=db.role_requests.find_one({'_id':ObjectId(request_id),'institution_id':institution_id,'status':'pending'})
    if not request:
        raise HTTPException(404,detail={'code':'REQUEST_NOT_FOUND','message':'Solicitud pendiente no encontrada'})
    if not can_review_requests(db,user,institution_id,request):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para rechazar esta solicitud'})
    reason=str(payload.get('reason','')).strip()
    if not 3<=len(reason)<=500:
        raise HTTPException(422,detail={'code':'INVALID_REASON','message':'Indica el motivo del rechazo (3 a 500 caracteres)'})
    now=now_utc()
    updated=db.role_requests.update_one(
        {'_id':request['_id'],'institution_id':institution_id,'status':'pending'},
        {'$set':{'status':'rejected','reviewed_by':str(user['_id']),'reviewed_at':now,'reason':reason}},
    )
    if updated.modified_count!=1:
        raise HTTPException(409,detail={'code':'REQUEST_ALREADY_REVIEWED','message':'La solicitud ya fue revisada'})
    db.institution_memberships.update_one(
        {'institution_id':institution_id,'user_id':str(request.get('user_id'))},
        {'$set':{'status':'rejected','updated_at':now}},
    )
    audit_institution_action(db,user,institution_id,'reject','role_requests',str(request['_id']),{'reason':reason})
    return {'success':True,'data':{'id':str(request['_id']),'status':'rejected'}}

@router.patch('/institutions/{institution_id}/settings')
def update_settings(institution_id:str,payload:dict,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id,active=False)
    if not is_institution_admin(db,user,institution_id): raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado'})
    changes={}
    if 'period_type' in payload:
        if payload['period_type'] not in ('quarter','semester'): raise HTTPException(422,detail={'code':'INVALID_PERIOD','message':'Periodo inválido'})
        changes['period_type']=payload['period_type']
    if not changes: raise HTTPException(422,detail={'code':'EMPTY_UPDATE','message':'Sin cambios permitidos'})
    changes['updated_at']=now_utc()
    db.institutions.update_one({'_id':ObjectId(institution_id)},{'$set':changes})
    return {'success':True,'data':{'id':institution_id,**{k:v for k,v in changes.items() if k!='updated_at'}}}

def upsert_staff_profile(collection, institution_id, career_id, user_id, now):
    collection.update_one(
        {'user_id': user_id, 'career_id': career_id, 'institution_id': {'$in': [institution_id, None]}},
        {'$set': {'institution_id': institution_id, 'career_id': career_id, 'is_active': True,
                  'is_deleted': False, 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


@router.post('/institutions/{institution_id}/role-assignments',status_code=201)
def assign_role(institution_id:str,payload:dict,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id,active=False)
    target=payload.get('user_id'); role=payload.get('role'); scope=payload.get('scope_type','institution'); scope_id=payload.get('scope_id')
    if not isinstance(target,str) or not ObjectId.is_valid(target) or role not in ('institution_admin','career_director','career_admin','teacher','tutor','student') or scope not in ('institution','career','group','class'):
        raise HTTPException(422,detail={'code':'INVALID_ASSIGNMENT','message':'Asignación inválida'})
    platform_actor=user.get('role') in ('admin','super_admin','platform_super_admin') and not user.get('institution_id')
    institution_admin=is_institution_admin(db,user,institution_id)
    director=None
    if not platform_actor and not institution_admin:
        director=career_director_assignment(db, user, institution_id)
        if not director or role not in ('teacher','tutor','student'):
            raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para asignar este rol'})
    if scope=='institution' and scope_id is not None or scope!='institution' and (not isinstance(scope_id,str) or not ObjectId.is_valid(scope_id)):
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'Ámbito inválido'})
    if role=='institution_admin' and scope!='institution':
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'El administrador institucional requiere ámbito de institución'})
    if role in ('career_director','career_admin','student') and scope!='career':
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'Este rol requiere ámbito de carrera'})
    if role=='tutor' and scope not in ('career','group'):
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'El tutor requiere ámbito de carrera o grupo'})
    if role=='teacher' and scope not in ('career','class'):
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'El docente requiere ámbito de carrera o clase'})
    member=db.institution_memberships.find_one({'institution_id':institution_id,'user_id':target,'status':'active'})
    if not member: raise HTTPException(409,detail={'code':'NOT_A_MEMBER','message':'El usuario no pertenece a la institución'})
    target_user=db.users.find_one({'_id':ObjectId(target),'is_active':True})
    if not target_user: raise HTTPException(409,detail={'code':'USER_UNAVAILABLE','message':'El usuario no está activo'})
    career_id=None
    if scope=='career':
        career_id=scope_id
    elif scope in ('group','class'):
        collection='groups' if scope=='group' else 'classes'
        resource=db[collection].find_one({'_id':ObjectId(scope_id),'institution_id':{'$in':[institution_id,None]},'is_deleted':{'$ne':True}})
        if not resource:
            raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'Recurso no encontrado'})
        career_id=str(resource.get('career_id',''))
    if scope != 'institution' and not institution_career(db,institution_id,career_id):
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'El alcance no pertenece a la institución'})
    if role=='institution_admin' and user.get('role') not in ('admin','super_admin','platform_super_admin','institution_owner'):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'Solo plataforma o el propietario pueden nombrar administradores institucionales'})
    if director:
        if str(director.get('scope_id'))!=str(career_id):
            raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No puedes asignar roles fuera de tu carrera'})
    if role=='student' and target_user.get('career_id') and str(target_user['career_id'])!=str(career_id):
        raise HTTPException(409,detail={'code':'CAREER_MISMATCH','message':'El usuario pertenece a otra carrera'})
    query={'institution_id':institution_id,'user_id':target,'role':role,'scope_type':scope,'scope_id':scope_id}
    now=now_utc()
    assignment_changes={'status':'active','granted_by':str(user['_id']),'granted_at':now,'assigned_by':str(user['_id']),'assigned_at':now}
    if career_id:
        assignment_changes['career_id']=career_id
    db.role_assignments.update_one(query,{'$set':assignment_changes},upsert=True)
    profile_collection={'teacher':'teachers','tutor':'tutors'}.get(role)
    if profile_collection:
        upsert_staff_profile(db[profile_collection], institution_id, career_id, target, now)
    audit_institution_action(db,user,institution_id,'assign_role','users',target,{'role':role,'scope_type':scope,'scope_id':scope_id})
    return {'success':True,'data':{**query,**({'career_id':career_id} if career_id else {})}}

@router.delete('/institutions/{institution_id}/role-assignments')
def revoke_role(institution_id:str,payload:dict,user=Depends(get_current_user)):
    db=db_required(); institution(db,institution_id,active=False)
    target=payload.get('user_id'); role=payload.get('role'); scope=payload.get('scope_type'); scope_id=payload.get('scope_id')
    if not isinstance(target,str) or not ObjectId.is_valid(target) or role not in ('institution_owner','institution_admin','career_director','career_admin','teacher','tutor','student') or scope not in ('institution','career','group','class'):
        raise HTTPException(422,detail={'code':'INVALID_ASSIGNMENT','message':'Asignación inválida'})
    if (scope=='institution' and scope_id is not None) or (scope!='institution' and (not isinstance(scope_id,str) or not ObjectId.is_valid(scope_id))):
        raise HTTPException(422,detail={'code':'INVALID_SCOPE','message':'Ámbito inválido'})
    platform_actor=user.get('role') in ('admin','super_admin','platform_super_admin') and not user.get('institution_id')
    if not platform_actor and (role=='institution_owner' or not is_institution_admin(db,user,institution_id)):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'No autorizado para revocar este rol'})
    assignment_query={
        'institution_id':institution_id,
        'user_id':target,
        'role':role,
        'scope_type':scope,
        'scope_id':scope_id,
        'status':'active',
    }
    if role=='institution_owner' and db.role_assignments.count_documents({
        'institution_id':institution_id,'role':'institution_owner','scope_type':'institution','scope_id':None,'status':'active',
    })<=1:
        raise HTTPException(409,detail={'code':'LAST_OWNER','message':'Asigna otro propietario antes de revocar al único propietario institucional'})
    now=now_utc()
    result=db.role_assignments.update_one(
        assignment_query,
        {'$set':{'status':'revoked','revoked_by':str(user['_id']),'revoked_at':now}},
    )
    if result.modified_count!=1:
        raise HTTPException(404,detail={'code':'ASSIGNMENT_NOT_FOUND','message':'La asignación activa no existe'})
    audit_institution_action(db,user,institution_id,'revoke_role','users',target,{'role':role,'scope_type':scope,'scope_id':scope_id})
    return {'success':True,'data':{'user_id':target,'role':role,'scope_type':scope,'scope_id':scope_id,'status':'revoked'}}

@router.get('/institutions/{institution_id}/me')
def institution_me(institution_id:str,user=Depends(get_current_user)):
    db=db_required(); inst=institution(db,institution_id)
    if user.get('institution_id')!=institution_id and not is_institution_admin(db,user,institution_id):
        raise HTTPException(403,detail={'code':'FORBIDDEN','message':'Institución no autorizada'})
    member=db.institution_memberships.find_one({'institution_id':institution_id,'user_id':str(user['_id'])})
    return {'success':True,'data':{'institution':public_institution(inst),'membership_status':member.get('status') if member else None,'account_type':user.get('account_type',user.get('role'))}}

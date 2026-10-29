from datetime import datetime,timezone
from pymongo.errors import DuplicateKeyError
from app.database import get_database
from app.security import hash_password
def ensure_user_indexes(db):
 db.users.create_index("email",unique=True)
 db.users.create_index([("role",1),("is_active",1)])
def create_user(*,email,password,role,full_name,career_id=None):
 db=get_database()
 if db is None: raise RuntimeError("MongoDB no está disponible")
 ensure_user_indexes(db); now=datetime.now(timezone.utc)
 document={"email":email.lower().strip(),"password_hash":hash_password(password),"role":role,"full_name":full_name.strip(),"is_active":True,"created_at":now,"updated_at":now}
 if career_id is not None: document["career_id"]=career_id
 try: result=db.users.insert_one(document)
 except DuplicateKeyError: raise ValueError("Ya existe un usuario con ese correo")
 document["_id"]=result.inserted_id
 return document
def public_user(user):
 return {"id":str(user["_id"]),"email":user["email"],"role":user["role"],"full_name":user["full_name"],"is_active":user.get("is_active",False),"career_id":user.get("career_id"),"created_at":user["created_at"], "avatar":user.get("avatar"), "bio":user.get("bio", ""), "banner_color":user.get("banner_color", "#63B89F")}

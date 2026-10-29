from app.config import get_settings
from app.database import get_database,ensure_indexes
from app.services.users import create_user,ensure_user_indexes
def ensure_bootstrap_admin():
 s=get_settings()
 if not s.mongodb_uri or not s.bootstrap_admin_email or not s.bootstrap_admin_password: return
 db=get_database()
 if db is None: return
 ensure_indexes(db); ensure_user_indexes(db)
 email=s.bootstrap_admin_email.lower().strip()
 if db.users.find_one({"email":email}) is None:
  create_user(email=email,password=s.bootstrap_admin_password,role="super_admin",full_name="Administrador inicial")

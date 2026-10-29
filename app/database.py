from pymongo import MongoClient
from pymongo.errors import PyMongoError
from app.config import get_settings
_client: MongoClient | None = None

def connect_database() -> None:
    global _client
    settings = get_settings()
    if not settings.mongodb_uri:
        return
    if _client is not None:
        return
    _client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                          socketTimeoutMS=10000, maxPoolSize=100, minPoolSize=2, waitQueueTimeoutMS=5000,
                          appname="CheckU-FastAPI", retryWrites=True)

def close_database() -> None:
    global _client
    if _client is not None:
        _client.close(); _client = None

def get_client():
    return _client

def get_database():
    client = get_client()
    return client[get_settings().mongodb_database] if client is not None else None

def ping_database() -> bool:
    client = get_client()
    if client is None: return False
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False

def ensure_indexes(db) -> None:
    db.users.create_index("email", unique=True)
    db.users.create_index([("role", 1), ("is_active", 1)])
    db.refresh_sessions.create_index("expires_at", expireAfterSeconds=0)
    db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)
    db.qr_challenges.create_index("expires_at", expireAfterSeconds=0)
    db.attendance.create_index([("session_id", 1), ("student_id", 1)], unique=True)
    db.enrollments.create_index([("student_id", 1), ("group_id", 1), ("period_id", 1)], unique=True)
    db.teaching_assignments.create_index([("teacher_id", 1), ("subject_id", 1), ("group_id", 1), ("period_id", 1)], unique=True)
    db.classes.create_index("join_code", unique=True)
    db.class_memberships.create_index([("class_id", 1), ("student_id", 1)], unique=True)
    db.class_memberships.create_index([('student_id', 1), ('subject_period_key', 1)],
        unique=True, partialFilterExpression={'subject_period_key': {'$type': 'string'}})
    db.class_tutor_invitations.create_index([('class_id', 1), ('tutor_id', 1)], unique=True)
    db.audit_logs.create_index([("created_at", -1)])

from base64 import b64encode
from io import BytesIO
from types import SimpleNamespace

from bson import ObjectId
from PIL import Image
from fastapi import HTTPException
import pytest

from app.routers.academic import attendance, classes, justifications


def test_institutional_tutor_assignment_includes_dual_role_account():
    group_id, tutor_id = ObjectId(), ObjectId()
    queries = {}

    def collection(name, row):
        def find(query, projection):
            queries[name] = query
            return [row]
        return SimpleNamespace(find=find)

    db = SimpleNamespace(
        role_assignments=collection("roles", {"user_id": str(tutor_id)}),
        institution_memberships=collection("members", {"user_id": str(tutor_id)}),
        users=collection("users", {"_id": tutor_id, "role": "teacher"}),
    )
    assert classes.assigned_tutor_ids(db, {"group_id": group_id}, "institution-1") == [tutor_id]
    assert queries["roles"] == {
        "institution_id": "institution-1", "role": "tutor", "scope_type": "group",
        "scope_id": {"$in": [str(group_id), group_id]}, "status": "active",
    }
    assert queries["members"]["institution_id"] == "institution-1"
    assert "role" not in queries["users"]


def test_tutor_inbox_is_restricted_to_its_scoped_sessions_and_recipients(monkeypatch):
    tutor_id, class_id, session_id = ObjectId(), ObjectId(), ObjectId()
    tutor = {"role": "tutor", "_id": tutor_id}
    monkeypatch.setattr(justifications, "scoped_classes", lambda db, user: [{"_id": class_id}])

    def sessions(query, projection):
        assert query == {"class_id": {"$in": [str(class_id), class_id]}}
        return [{"_id": session_id}]

    db = SimpleNamespace(class_sessions=SimpleNamespace(find=sessions))
    query = justifications.request_scope(db, tutor)
    assert query["session_id"] == {"$in": [str(session_id), session_id]}
    assert query["$or"] == [
        {"tutor_id": {"$in": [str(tutor_id), tutor_id]}},
        {"tutor_ids": {"$in": [str(tutor_id), tutor_id]}},
    ]
    assert justifications.review_filter(tutor) == {"status": {"$in": justifications.OPEN}}


def test_only_recipient_role_can_resolve_and_evidence_is_normalized():
    teacher, tutor = {"role": "teacher"}, {"role": "tutor"}
    incoming = {"submitted_by_role": "teacher"}
    assert justifications.can_resolve(tutor, incoming)
    assert not justifications.can_resolve(teacher, incoming)
    assert not justifications.can_resolve(teacher, {"submitted_by_role": "student"})
    assert not justifications.can_resolve({"role": "student"}, incoming)
    assert justifications.can_resolve(tutor, {"submitted_by_role": "student"})

    raw = BytesIO()
    Image.new("RGB", (2, 2)).save(raw, format="PNG")
    assert justifications.image_data(b64encode(raw.getvalue()).decode()).startswith("data:image/jpeg;base64,")


def test_legacy_automatic_absence_does_not_override_a_check_in():
    profile = ObjectId()
    class Cursor(list):
        def sort(self, keys):
            return self

    check_in = {"student_id": str(profile), "status": "present"}
    db = SimpleNamespace(
        students=SimpleNamespace(find=lambda query, projection: [{"_id": profile}]),
        attendance=SimpleNamespace(find=lambda query: Cursor([
            {"status": "absent", "created_by": "system"}, check_in,
        ])),
    )
    assert attendance.attendance_for_student(db, str(ObjectId()), str(ObjectId())) == check_in



def test_student_can_only_create_for_own_account(monkeypatch):
    student_id, other_id, tutor_id, class_id, session_id = (ObjectId() for _ in range(5))
    student = {"role": "student", "_id": student_id}
    cls = {"_id": class_id}
    session = {"_id": session_id, "class_id": class_id, "status": "closed"}
    saved = []

    def membership(query):
        assert query["student_id"]["$in"] == [str(student_id), student_id]
        return {}

    db = SimpleNamespace(
        class_sessions=SimpleNamespace(find_one=lambda query: session),
        class_memberships=SimpleNamespace(find_one=lambda query: membership(query) or {"student_id": student_id}),
        users=SimpleNamespace(find_one=lambda query: {"_id": student_id} if query["_id"] == student_id else None),
        justifications=SimpleNamespace(insert_one=lambda doc: (saved.append(doc), SimpleNamespace(inserted_id=ObjectId()))[1]),
    )
    monkeypatch.setattr(justifications, "dbx", lambda: db)
    monkeypatch.setattr(justifications, "scoped_classes", lambda db, user: [cls])
    monkeypatch.setattr(justifications, "assigned_tutor_ids", lambda *args, **kwargs: [tutor_id])
    monkeypatch.setattr(justifications, "attendance_for_student", lambda *args: {"status": "absent"})
    monkeypatch.setattr(justifications, "audit", lambda *args: None)
    payload = {"session_id": str(session_id), "student_id": str(other_id), "reason": "Consulta médica"}
    justifications.create(payload, student)
    assert saved[0]["student_user_id"] == student_id
    assert saved[0]["tutor_ids"] == [str(tutor_id)]
    with pytest.raises(HTTPException) as denied:
        justifications.create(payload, {"role": "teacher", "_id": tutor_id})
    assert denied.value.status_code == 403


def test_student_request_scope_uses_owner_and_institution_classes(monkeypatch):
    student_id, class_id, session_id = ObjectId(), ObjectId(), ObjectId()
    student = {"role": "student", "_id": student_id, "institution_id": "inst"}
    monkeypatch.setattr(justifications, "scoped_classes", lambda db, user: [{"_id": class_id}])

    def sessions(query, projection):
        assert query["class_id"]["$in"] == [str(class_id), class_id]
        return [{"_id": session_id}]

    db = SimpleNamespace(class_sessions=SimpleNamespace(find=sessions))
    assert justifications.request_scope(db, student) == {
        "$or": [
            {"student_user_id": {"$in": [str(student_id), student_id]}},
            {"student_user_id": None, "student_id": {"$in": [str(student_id), student_id]}},
        ],
        "session_id": {"$in": [str(session_id), session_id]},
    }



def test_student_detail_keeps_history_and_hides_internal_conversation(monkeypatch):
    student, tutor, teacher, session, subject, jid = (ObjectId() for _ in range(6))
    users = SimpleNamespace(find=lambda query, projection: [
        {"_id": student, "full_name": "Alumno de prueba"},
        {"_id": tutor, "full_name": "Tutor de prueba"},
        {"_id": teacher, "full_name": "Docente de prueba"},
    ])
    from datetime import datetime, timezone
    started = datetime(2025, 1, 3, 18, 30, tzinfo=timezone.utc)
    resolved = datetime(2025, 1, 4, 18, tzinfo=timezone.utc)
    doc = {
        "_id": jid, "session_id": str(session), "teacher_id": str(teacher),
        "tutor_id": str(tutor), "resolved_by": str(teacher), "submitted_by_role": "tutor",
        "status": "approved", "created_at": started, "resolved_at": resolved,
        "resolution_comment": "Comentario interno",
        "messages": [
            {"id": "internal", "author_role": "teacher", "author_id": str(teacher), "message": "Interno", "student_visible": True},
            {"id": "staff", "author_role": "tutor", "author_id": str(tutor), "message": "Conversacion privada"},
            {"id": "own", "author_role": "student", "author_id": str(student), "message": "Mi solicitud"},
            {"id": "shared", "author_role": "tutor", "author_id": str(tutor), "message": "Respuesta", "student_visible": True},
        ],
    }
    db = SimpleNamespace(
        users=users,
        class_sessions=SimpleNamespace(find_one=lambda query, projection: {"started_at": started, "subject_id": subject}),
        subjects=SimpleNamespace(find_one=lambda query, projection: {"name": "Matematicas"}),
        justifications=SimpleNamespace(find_one=lambda query: doc),
    )
    viewer = {"_id": student, "role": "student"}
    monkeypatch.setattr(justifications, "dbx", lambda: db)
    monkeypatch.setattr(justifications, "can_access", lambda db, user, doc: True)
    data = justifications.get_one(str(jid), viewer)["data"]
    assert data["submitted_by_name"] == "Tutor de prueba"
    assert data["teacher_name"] == data["resolved_by_name"] == "Docente de prueba"
    assert data["session_at"] == started and data["resolved_at"] == resolved
    assert data["messages"] == [] and "resolution_comment" not in data
    with pytest.raises(HTTPException) as denied:
        justifications.send_message(str(jid), {"message": "Intento de compartir"}, viewer)
    assert denied.value.status_code == 403
    doc["submitted_by_role"] = "student"
    messages = justifications.messages(str(jid), viewer)["data"]
    assert [entry["id"] for entry in messages] == ["own", "shared"]
    assert all("author_id" not in entry for entry in messages)

from base64 import b64encode
from io import BytesIO
from types import SimpleNamespace

from bson import ObjectId
from PIL import Image

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
    assert justifications.review_filter(tutor)["submitted_by_role"] == "teacher"


def test_only_recipient_role_can_resolve_and_evidence_is_normalized():
    teacher, tutor = {"role": "teacher"}, {"role": "tutor"}
    incoming = {"submitted_by_role": "teacher"}
    assert justifications.can_resolve(tutor, incoming)
    assert not justifications.can_resolve(teacher, incoming)
    assert justifications.can_resolve(teacher, {})
    assert not justifications.can_resolve(tutor, {})

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

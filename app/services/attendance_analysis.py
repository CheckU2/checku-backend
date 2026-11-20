"""Attendance rollups from closed sessions in the actor's class scope."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import HTTPException

from app.routers.academic.attendance import as_utc, is_automatic_absence
from app.routers.academic.classes import assigned_tutor_ids, id_refs, scope

ZONE = ZoneInfo("America/Mexico_City")
PRESENT = {"present", "late", "excused"}
STATUSES = ("present", "late", "excused", "absent", "pending_review", "invalidated")
LABELS = dict(zip(STATUSES, ("Presente", "Retardo", "Justificada", "Falta", "En revisión", "Invalidada")))


def refs(values):
    return [ref for value in values for ref in id_refs(value)]


def lookup(collection, values, field="name"):
    ids = [ObjectId(value) for value in set(map(str, values)) if ObjectId.is_valid(value)]
    return {str(row["_id"]): row.get(field) or row.get("code") or "" for row in collection.find({"_id": {"$in": ids}}, {field: 1, "code": 1})}


def percentage(counts):
    total = sum(counts.values())
    return round(100 * sum(counts.get(status, 0) for status in PRESENT) / total, 1) if total else None


def attendance_records(db, sessions, students):
    aliases = {str(profile["_id"]): str(profile["user_id"]) for profile in
               db.students.find({"user_id": {"$in": refs(students)}}, {"user_id": 1})}
    records = {}
    for record in db.attendance.find({"session_id": {"$in": refs(sessions)}}).sort([("updated_at", 1), ("recorded_at", 1)]):
        uid = str(record.get("user_id") or record.get("student_id"))
        key = (str(record["session_id"]), aliases.get(uid, uid))
        if key not in records or not is_automatic_absence(record) or is_automatic_absence(records[key]):
            records[key] = record
    return records


def academic_report(db, user, days, group_id=None, class_id=None, student_id=None, career_id=None, metadata=False):
    if user["role"] not in {"teacher", "tutor", "career_admin", "institution_owner", "institution_admin", "super_admin", "admin"}:
        raise HTTPException(403, detail={"message": "Tu rol no puede consultar este reporte"})
    classes = list(db.classes.find({"$and": [scope(db, user), {"is_deleted": {"$ne": True}}]}))
    by_class = {str(row["_id"]): row for row in classes}
    names = {
        "group": lookup(db.groups, (row.get("group_id") for row in classes)),
        "subject": lookup(db.subjects, (row.get("subject_id") for row in classes)),
        "career": lookup(db.careers, (row.get("career_id") for row in classes)),
        "period": lookup(db.academic_periods, (row.get("period_id") for row in classes)),
        "teacher": lookup(db.users, (row.get("teacher_id") for row in classes), "full_name"),
    }

    def field(row, kind):
        return names[kind].get(str(row.get(f"{kind}_id")), "") or "Sin asignar"

    options = [{
        "id": cid, "group_id": str(row.get("group_id")), "career_id": str(row.get("career_id")),
        **{kind: field(row, kind) for kind in names},
    } for cid, row in by_class.items()]
    selected = {item["id"] for item in options if (not group_id or item["group_id"] == group_id)
                and (not class_id or item["id"] == class_id) and (not career_id or item["career_id"] == career_id)}
    if (group_id or class_id or career_id) and not selected:
        raise HTTPException(404, detail={"message": "No se encontró ese grupo o clase en tu ámbito"})
    ids = refs(by_class)
    enrollment = defaultdict(dict)
    for member in db.class_memberships.find({"class_id": {"$in": ids}}, {"class_id": 1, "student_id": 1, "joined_at": 1}):
        enrollment[str(member["class_id"])][str(member["student_id"])] = member.get("joined_at")
    roster = set().union(*(enrollment[cid] for cid in selected))
    if student_id and student_id not in roster:
        raise HTTPException(404, detail={"message": "Ese alumno no pertenece a la selección"})
    people = lookup(db.users, roster, "full_name")

    session_filter = {"class_id": {"$in": ids}, "status": "closed",
                      "ended_at": {"$type": "date"}}
    if days:
        session_filter["ended_at"]["$gte"] = datetime.now(timezone.utc) - timedelta(days=days)
    sessions = list(db.class_sessions.find(session_filter).sort("ended_at", 1))
    records = attendance_records(db, (row["_id"] for row in sessions), set().union(*enrollment.values()))

    comparison, daily, heat, counts = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    details = []
    for session in sessions:
        cid, sid = str(session["class_id"]), str(session["_id"])
        cls = by_class[cid]
        end = as_utc(session["ended_at"])
        local = as_utc(session.get("started_at") or session["ended_at"]).astimezone(ZONE)
        marks = {}
        for uid, joined in enrollment[cid].items():
            if joined and as_utc(joined) > end:
                continue
            status = (records.get((sid, uid)) or {}).get("status") or "absent"
            if status not in STATUSES:
                status = "pending_review"
            if not career_id or str(cls.get("career_id")) == career_id:
                comparison[str(cls.get("group_id"))][status] += 1
            if cid not in selected or (student_id and uid != student_id):
                continue
            counts[uid][status] += 1
            daily[local.date().isoformat()][status] += 1
            heat[(local.weekday(), local.hour)][status] += 1
            if metadata:
                marks[uid] = status
        if metadata and cid in selected:
            details.append({"id": sid, "class_id": cid, "date": local.strftime("%Y-%m-%d %H:%M"), "marks": marks})

    learners = [{"id": uid, "name": people.get(uid) or "Alumno", "counts": dict(counts[uid]), "percentage": percentage(counts[uid])}
                for uid in sorted(roster, key=lambda uid: ((people.get(uid) or "").casefold(), uid)) if not student_id or uid == student_id]
    groups = [{"id": gid, "name": names["group"].get(gid) or "Grupo", "counts": dict(values), "percentage": percentage(values)}
              for gid, values in comparison.items()]
    result = {"classes": options, "students": learners, "groups": sorted(groups, key=lambda item: item["name"]),
              "daily": [{"date": key, "counts": dict(values), "percentage": percentage(values)} for key, values in daily.items()],
              "heat": [{"day": key[0], "hour": key[1], "absences": values.get("absent", 0), "total": sum(values.values())}
                       for key, values in heat.items()], "minimum": 80}
    if not metadata:
        selected_sessions = [session["_id"] for session in sessions if str(session["class_id"]) in selected]
        query = {"session_id": {"$in": refs(selected_sessions)}}
        if student_id:
            query["student_id"] = {"$in": id_refs(student_id)}
        records = db.justifications.find(query, {"student_id": 1, "reason": 1, "status": 1}).sort("created_at", -1)
        result["justifications"] = [
            {"id": str(row["_id"]), "name": people.get(str(row.get("student_id"))) or "Alumno",
             "reason": row.get("reason", ""), "status": row.get("status", "pending")}
            for row in records
        ]
    if metadata:
        result["sessions"] = details
        result["metadata"] = []
        tutors_by_group = {}
        for item in options:
            if item["id"] not in selected:
                continue
            cls = by_class[item["id"]]
            institution_id = user.get("institution_id") or cls.get("institution_id")
            career = db.careers.find_one({"_id": ObjectId(str(cls["career_id"]))}, {"institution_id": 1}) if ObjectId.is_valid(str(cls.get("career_id"))) else None
            institution_id = institution_id or (career or {}).get("institution_id")
            key = (institution_id, item["group_id"])
            if key not in tutors_by_group:
                tutors = assigned_tutor_ids(db, cls, institution_id)
                tutors_by_group[key] = ", ".join(sorted(lookup(db.users, tutors, "full_name").values()))
            university = lookup(db.institutions, [institution_id]).get(str(institution_id), "")
            result["metadata"].append({**item, "institution": university or "Sin institución registrada", "tutors": tutors_by_group[key] or "Sin tutor asignado"})
    return result

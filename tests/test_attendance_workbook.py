from io import BytesIO
from zipfile import ZipFile
from xml.etree.ElementTree import fromstring

from app.services.attendance_analysis import percentage
from app.services.attendance_workbook import workbook, MAIN


def test_attendance_percentage_and_workbook_keep_numeric_results():
    assert percentage({"present": 2, "absent": 1, "late": 1, "excused": 1}) == 80
    assert percentage({}) is None
    report = {
        "metadata": [{"id": "class", "institution": "Universidad", "career": "Carrera", "group": "4A",
                      "period": "2026", "subject": "Interfaces", "teacher": "Docente", "tutors": "Tutor"}],
        "students": [{"id": "a", "name": "=1+1", "counts": {"present": 1, "absent": 1}, "percentage": 50}],
        "sessions": [{"id": "s", "class_id": "class", "date": "2026-11-19 11:00", "marks": {"a": "absent"}}],
    }
    with ZipFile(BytesIO(workbook(report))) as spreadsheet:
        assert spreadsheet.testzip() is None
        assert "xl/styles.xml" in spreadsheet.namelist()
        xml = fromstring(spreadsheet.read("xl/worksheets/sheet1.xml"))
        prefix = f"{{{MAIN}}}"
        assert xml.find(f".//{prefix}c[@r='G7']/{prefix}v").text == "0.5"
        assert xml.find(f".//{prefix}c[@r='A7']").get("t") == "inlineStr"
        assert xml.find(f".//{prefix}c[@r='H7']/{prefix}is/{prefix}t").text == "Falta"

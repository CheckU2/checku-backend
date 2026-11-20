"""Write a compact XLSX report with inline strings and CheckU colors."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree.ElementTree import Element, SubElement, tostring

from app.services.attendance_analysis import LABELS

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def column(index):
    name = ""
    while index:
        index, value = divmod(index - 1, 26)
        name = chr(65 + value) + name
    return name


def workbook(report):
    data = [
        [("CheckU · Reporte de asistencia", 1)],
        [("Porcentaje mínimo", 1), ("80%", 1)],
        [(label, 1) for label in ("Universidad", "Carrera", "Grupo", "Periodo", "Materia", "Docente", "Tutores")],
    ]
    for item in report["metadata"]:
        data.append([(item[key], 0) for key in ("institution", "career", "group", "period", "subject", "teacher", "tutors")])
    data.append([])
    subjects = {item["id"]: item["subject"] for item in report["metadata"]}
    sessions = report["sessions"]
    data.append([(value, 1) for value in ["Alumno", "Presentes", "Retardos", "Justificadas", "Faltas", "Evaluadas", "Asistencia",
                                         *(f'{session["date"]} · {subjects[session["class_id"]]}' for session in sessions)]])
    for person in report["students"]:
        counts = person["counts"]
        percentage = person["percentage"]
        row = [(person["name"], 0)] + [(counts.get(status, 0), 0) for status in ("present", "late", "excused", "absent")]
        row += [(sum(counts.values()), 0), ((percentage / 100) if percentage is not None else "—", 2 if percentage is None or percentage >= 80 else 3)]
        for session in sessions:
            status = session["marks"].get(person["id"])
            row.append((LABELS.get(status, "—"), 4 if status == "absent" else 5 if status in {"present", "late", "excused"} else 0))
        data.append(row)

    sheet = Element("worksheet", xmlns=MAIN)
    cols = SubElement(sheet, "cols")
    SubElement(cols, "col", min="1", max="1", width="32", customWidth="1")
    SubElement(cols, "col", min="2", max="7", width="23", customWidth="1")
    SubElement(cols, "col", min="8", max=str(max(8, 7 + len(sessions))), width="36", customWidth="1")
    table = SubElement(sheet, "sheetData")
    for index, row in enumerate(data, 1):
        element = SubElement(table, "row", r=str(index))
        for position, (value, style) in enumerate(row, 1):
            cell = SubElement(element, "c", r=f"{column(position)}{index}", s=str(style))
            if isinstance(value, (int, float)):
                SubElement(cell, "v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                SubElement(SubElement(cell, "is"), "t").text = "".join(char for char in value if ord(char) >= 32 or char in "\n\t\r")

    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        base = 'http://schemas.openxmlformats.org/package/2006/relationships'
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{base}"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{base}"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{REL}/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets><sheet name="Asistencia" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/worksheets/sheet1.xml", tostring(sheet, encoding="utf-8"))
        fills = "".join(f'<fill><patternFill patternType="solid"><fgColor rgb="{color}"/><bgColor indexed="64"/></patternFill></fill>'
                        for color in ("FF57398E", "FFFFE3EB", "FFDDF2E8"))
        formats = "".join(f'<xf numFmtId="{fmt}" fontId="{font}" fillId="{fill}" borderId="0" xfId="0" applyFill="1" applyNumberFormat="1"/>'
                          for fmt, font, fill in ((0,0,0),(0,1,2),(10,0,4),(10,0,3),(0,0,3),(0,0,4)))
        archive.writestr("xl/styles.xml", f'<styleSheet xmlns="{MAIN}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>{fills}</fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6">{formats}</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    return stream.getvalue()

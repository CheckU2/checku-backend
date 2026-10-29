from math import floor

def attendance_percentage(*, present: int, late: int, excused: int, evaluable_sessions: int) -> float | None:
    if evaluable_sessions <= 0:
        return None
    if min(present, late, excused, evaluable_sessions) < 0:
        raise ValueError("Los conteos no pueden ser negativos")
    return round(100 * (present + late + excused) / evaluable_sessions, 2)

def derived_absences_from_confirmed_abandonments(confirmed_abandonments: int) -> int:
    if confirmed_abandonments < 0:
        raise ValueError("El número de abandonos no puede ser negativo")
    return floor(confirmed_abandonments / 3)

def is_at_risk(percentage: float | None, threshold: float = 80.0) -> bool:
    return percentage is not None and percentage < threshold

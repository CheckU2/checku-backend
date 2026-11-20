from pathlib import Path

def test_v2_disabled_by_default():
    source = Path('app/config.py').read_text()
    assert 'multi_institution_registration_enabled: bool = False' in source

def test_legacy_endpoints_remain():
    source = Path('app/routers/auth.py').read_text()
    for route in ('/register','/login','/refresh','/logout','/me'):
        assert route in source

def test_institutions_activated_on_creation():
    source = Path("app/routers/institutions.py").read_text(encoding="utf-8")
    assert "'status':'active'" in source

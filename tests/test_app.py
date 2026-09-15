import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, connect, create_app, export_once


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "poll.sqlite3"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ACCESS_CODE", "")
    monkeypatch.setenv("REFUSAL_DEMOGRAPHICS", "true")
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "")
    monkeypatch.setenv("SMS_GATEWAY_URL", "")
    monkeypatch.setenv("SMS_GATEWAY_TOKEN", "")
    return Settings()


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def survey(settings):
    now = datetime.now(timezone.utc)
    return {"id": str(uuid4()), "created_at": now.isoformat(),
            "profile": {"id": str(uuid4()), "surname": "Тестовый", "name": "Интервьюер",
                        "precinct": settings.precincts[0]["id"], "day": now.astimezone(settings.zone).date().isoformat()},
            "party": "11", "gender": "female", "age": "25–34"}


def test_duplicate_submit_is_one_row(client, settings, survey):
    assert client.post("/api/surveys", json=survey).status_code == 200
    assert client.post("/api/surveys", json=survey).status_code == 200
    with connect(settings) as db:
        assert db.execute("SELECT COUNT(*) FROM surveys").fetchone()[0] == 1


def test_same_id_different_answers_is_conflict(client, survey):
    client.post("/api/surveys", json=survey)
    survey["party"] = "8"
    assert client.post("/api/surveys", json=survey).status_code == 409


@pytest.mark.parametrize("changes", [
    {"party": "2"}, {"party": "99"}, {"age": "17"}, {"gender": "unknown"},
    {"age": None}, {"gender": None}, {"party": "refused", "age": None},
])
def test_invalid_answers_rejected(client, survey, changes):
    survey.update(changes)
    assert client.post("/api/surveys", json=survey).status_code == 422


@pytest.mark.parametrize("party", ["1","3","4","5","6","7","8","9","10","11","spoiled","refused"])
def test_all_enabled_answers_accepted(client, survey, party):
    survey["party"] = party
    assert client.post("/api/surveys", json=survey).status_code == 200


def test_unknown_precinct_rejected(client, survey):
    survey["profile"]["precinct"] = "not-existing"
    assert client.post("/api/surveys", json=survey).status_code == 422


def test_offline_yesterday_arrives_today(client, survey, settings):
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    survey["created_at"] = yesterday.isoformat()
    survey["profile"]["day"] = yesterday.astimezone(settings.zone).date().isoformat()
    assert client.post("/api/surveys", json=survey).status_code == 200


def test_day_mismatch_and_future_rejected(client, survey):
    survey["profile"]["day"] = "2000-01-01"
    assert client.post("/api/surveys", json=survey).status_code == 422
    survey["created_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert client.post("/api/surveys", json=survey).status_code == 422


def test_requires_timezone(client, survey):
    survey["created_at"] = "2026-09-15T12:00:00"
    assert client.post("/api/surveys", json=survey).status_code == 422


def test_sheets_timeout_retry_reuses_identical_range(client, survey, settings):
    client.post("/api/surveys", json=survey)
    settings.spreadsheet = "test"
    calls = []
    def ambiguous_timeout(body):
        calls.append(copy.deepcopy(body))
        raise TimeoutError("Google may have already accepted the write")
    with pytest.raises(TimeoutError):
        export_once(settings, ambiguous_timeout)
    with connect(settings) as db:
        assert db.execute("SELECT exported FROM surveys").fetchone()[0] == 0
    assert export_once(settings, lambda body: calls.append(copy.deepcopy(body))) == 1
    assert calls[0] == calls[1]
    assert calls[0]["data"][1]["range"] == "'Анкеты'!A2:M2"
    assert calls[0]["valueInputOption"] == "RAW"
    assert export_once(settings, lambda _: pytest.fail("Already exported")) == 0


def test_name_is_not_a_sheet_formula(client, survey, settings):
    survey["profile"]["name"] = '=IMPORTXML("example", "//x")'
    client.post("/api/surveys", json=survey)
    settings.spreadsheet = "test"
    calls = []
    export_once(settings, calls.append)
    assert calls[0]["valueInputOption"] == "RAW"
    assert calls[0]["data"][1]["values"][0][4].startswith("=IMPORTXML")


def test_auth_and_cross_origin(settings, survey):
    settings.access_code = "test-code"
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/surveys", json=survey).status_code == 401
        assert client.post("/api/login", json={"code":"wrong"}).status_code == 401
        assert client.post("/api/login", json={"code":"test-code"}).status_code == 200
        assert client.post("/api/surveys", json=survey).status_code == 200
        assert client.post("/api/surveys", json=survey, headers={"Origin":"https://attacker.example"}).status_code == 403


def test_sms_unconfigured_does_not_claim_success(client):
    assert client.post("/api/sms/request", json={"phone":"+79000000000"}).status_code == 503


def test_sms_provider_contract_and_rate_limit(settings, monkeypatch):
    calls = []
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"accepted": True}
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeClient)
    settings.sms_url = "https://test.example/send"
    settings.sms_token = "test-token"
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/sms/request", json={"phone":"invalid"}).status_code == 422
        assert client.post("/api/sms/request", json={"phone":"+79000000000"}).json() == {"accepted":True}
        assert client.post("/api/sms/request", json={"phone":"+79000000000"}).status_code == 429
    assert len(calls) == 1
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-token"
    assert "ответы на SMS не обрабатываются" in calls[0][1]["json"]["text"]


def test_static_app_is_available(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/sw.js").headers["content-type"].startswith("application/javascript")
    config = client.get("/api/config").json()
    assert config["refusal_demographics"] is True
    assert [x["id"] for x in config["parties"] if x["disabled"]] == ["2"]


def test_production_rejects_missing_credentials(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ACCESS_CODE", "")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        Settings()


def test_production_rejects_demo_precincts(monkeypatch, tmp_path):
    demo = tmp_path / 'demo.json'
    demo.write_text('[{"id":"demo-001","label":"Demo"}]')
    monkeypatch.setenv('PRECINCTS_FILE', str(demo))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ACCESS_CODE", "a-long-test-team-code")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    with pytest.raises(RuntimeError, match="demonstration precincts"):
        Settings()


def test_catalog_has_all_pdf_entries(client):
    catalog = client.get('/api/config').json()['precincts']
    assert len(catalog) == 3925
    assert len({p['id'] for p in catalog}) == 3925
    assert len({p['tik'] for p in catalog}) == 56
    by_id = {p['id']: p for p in catalog}
    assert by_id['mo-uik-1']['tik'] == 'ТИК города Балашиха'
    assert by_id['mo-uik-89']['tik'] == 'ТИК города Бронницы'
    assert by_id['mo-uik-3765']['tik'] == 'ТИК поселка Звёздный городок'


def test_tik_must_match_precinct(client, survey, settings):
    survey['profile']['tik'] = 'ТИК города Бронницы'
    assert client.post('/api/surveys', json=survey).status_code == 422
    survey['profile']['tik'] = settings.precincts[0]['tik']
    assert client.post('/api/surveys', json=survey).status_code == 200


def test_tik_export_is_new_column(client, survey, settings):
    survey['profile']['tik'] = settings.precincts[0]['tik']
    client.post('/api/surveys', json=survey)
    settings.spreadsheet = 'test'
    calls = []
    export_once(settings, calls.append)
    assert calls[0]['data'][0]['values'][0][-1] == 'ТИК'
    assert calls[0]['data'][1]['values'][0][12] == settings.precincts[0]['tik']
    assert calls[0]['data'][1]['values'][0][7] == 'Новые люди'


def test_export_after_db_reset_preserves_existing_sheet(client, survey, settings):
    client.post('/api/surveys', json=survey)
    settings.spreadsheet = 'test'
    calls = []
    export_once(settings, calls.append, read_ids=lambda: [['old-1'], [], ['old-3']])
    assert calls[0]['data'][1]['range'] == "'Анкеты'!A5:M5"


def test_retry_after_accepted_timeout_finds_existing_id(client, survey, settings):
    client.post('/api/surveys', json=survey)
    settings.spreadsheet = 'test'
    remote = [['old-1']]
    calls = []
    def timeout(body):
        calls.append(copy.deepcopy(body))
        remote.append([survey['id']])
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        export_once(settings, timeout, read_ids=lambda: remote)
    export_once(settings, calls.append, read_ids=lambda: remote)
    assert calls[0] == calls[1]
    assert calls[1]['data'][1]['range'] == "'Анкеты'!A3:M3"


def test_sheet_read_failure_does_not_write(client, survey, settings):
    client.post('/api/surveys', json=survey)
    settings.spreadsheet = 'test'
    def read_failure():
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        export_once(settings, lambda _: pytest.fail('Must not write'), read_ids=read_failure)
    with connect(settings) as db:
        assert db.execute('SELECT exported FROM surveys').fetchone()[0] == 0


def test_old_demo_queue_still_accepted(client, survey, settings):
    survey['profile']['precinct'] = 'demo-001'
    assert client.post('/api/surveys', json=survey).status_code == 200
    assert client.post('/api/surveys', json=survey).status_code == 200
    settings.spreadsheet = 'test'
    calls = []
    export_once(settings, calls.append)
    assert calls[0]['data'][1]['values'][0][-1] == ''

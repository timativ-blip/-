import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import OKRUGS, Settings, TIK_TO_OKRUG, connect, create_app, dashboard_snapshot, export_once


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
    assert client.get("/dashboard").status_code == 200


def test_dashboard_aggregates_sheet_rows(settings):
    tik = settings.precincts[0]["tik"]
    precinct = settings.precincts[0]
    values = [
        ["one", "2026-09-16T08:30:00+00:00", "2026-09-16", "Иванова", "Анна",
         precinct["id"], precinct["label"], "Новые люди", "Женский", "25–34", "shift-1", "", tik],
        ["two", "2026-09-16T09:00:00+00:00", "2026-09-16", "Иванова", "Анна",
         precinct["id"], precinct["label"], "Отказался отвечать", "Мужской", "45–60", "shift-1", "", tik],
    ]
    result = dashboard_snapshot(values, settings)
    assert result["selected_day"] == "2026-09-16"
    assert result["summary"] == {"total": 2, "refusals": 1, "spoiled": 0,
                                  "interviewers": 1, "uiks": 1, "tiks": 1}
    assert next(x for x in result["parties"] if x["label"] == "Новые люди")["count"] == 1
    assert result["interviewers"][0]["total"] == 2

    precinct_result = dashboard_snapshot(values, settings, requested_tik=tik,
                                         requested_precinct=precinct["id"])
    assert [item["id"] for item in precinct_result["uik_stats"]] == [precinct["id"]]
    assert precinct_result["uik_stats"][0]["spoiled"] == 0


def test_dashboard_all_days_combines_totals(settings):
    tik = settings.precincts[0]["tik"]
    precinct = settings.precincts[0]
    values = [
        ["one", "2026-09-15T08:30:00+00:00", "2026-09-15", "Иванова", "Анна",
         precinct["id"], precinct["label"], "Новые люди", "Женский", "25–34", "shift-1", "", tik],
        ["two", "2026-09-16T09:00:00+00:00", "2026-09-16", "Иванова", "Анна",
         precinct["id"], precinct["label"], "Отказался отвечать", "Мужской", "45–60", "shift-1", "", tik],
    ]
    single_day = dashboard_snapshot(values, settings, requested_day="2026-09-16")
    assert single_day["summary"]["total"] == 1
    all_days = dashboard_snapshot(values, settings, requested_day="all")
    assert all_days["selected_day"] == "all"
    assert all_days["summary"]["total"] == 2
    assert next(x for x in all_days["parties"] if x["label"] == "Новые люди")["count"] == 1
    assert sum(item["total"] for item in all_days["okrug_stats"]) == 2


def test_dashboard_all_days_accepted_by_endpoint(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        response = client.get("/api/dashboard/data?day=all")
        assert response.status_code == 200
        assert response.json()["selected_day"] == "all"


def test_okrugs_cover_every_precinct_tik(settings):
    tiks = {p["tik"] for p in settings.precincts if p.get("tik")}
    assert tiks <= set(TIK_TO_OKRUG)
    assert len(OKRUGS) == 12


def test_okrug_groups_tiks_and_drills_down(settings):
    balashikha = next(p for p in settings.precincts if p["tik"] == "ТИК города Балашиха")
    dmitrov = next(p for p in settings.precincts if p["tik"] == "ТИК города Дмитров")
    values = [
        ["one", "2026-09-16T08:00:00+00:00", "2026-09-16", "А", "Б",
         balashikha["id"], balashikha["label"], "Новые люди", "Женский", "25–34", "shift-1", "", "ТИК города Балашиха"],
        ["two", "2026-09-16T09:00:00+00:00", "2026-09-16", "В", "Г",
         dmitrov["id"], dmitrov["label"], "Единая Россия", "Мужской", "45–60", "shift-2", "", "ТИК города Дмитров"],
    ]
    top = dashboard_snapshot(values, settings, requested_day="2026-09-16")
    assert top["tik_stats"] == []
    okrug_118 = next(x for x in top["okrug_stats"] if x["okrug"] == "118")
    okrug_119 = next(x for x in top["okrug_stats"] if x["okrug"] == "119")
    assert okrug_118["total"] == 1
    assert okrug_119["total"] == 1
    assert len(top["okrug_stats"]) == 12

    drilled = dashboard_snapshot(values, settings, requested_day="2026-09-16", requested_okrug="118")
    assert drilled["summary"]["total"] == 1
    assert drilled["filters"]["tiks"] == sorted(OKRUGS["118"])
    nonzero = [item["tik"] for item in drilled["tik_stats"] if item["total"]]
    assert nonzero == ["ТИК города Балашиха"]


def test_okrug_endpoint_validation(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        assert client.get("/api/dashboard/data", params={"okrug": "999"}).status_code == 422
        assert client.get("/api/dashboard/data",
                           params={"okrug": "118", "tik": "ТИК города Дмитров"}).status_code == 422
        assert client.get("/api/dashboard/data", params={"okrug": "118"}).status_code == 200


def test_parties_sorted_by_count_desc(settings):
    precinct = settings.precincts[0]
    tik = precinct["tik"]
    values = [
        ["one", "2026-09-16T08:00:00+00:00", "2026-09-16", "А", "Б",
         precinct["id"], precinct["label"], "ЛДПР", "Мужской", "25–34", "s1", "", tik],
        ["two", "2026-09-16T08:05:00+00:00", "2026-09-16", "А", "Б",
         precinct["id"], precinct["label"], "Единая Россия", "Мужской", "25–34", "s1", "", tik],
        ["three", "2026-09-16T08:10:00+00:00", "2026-09-16", "А", "Б",
         precinct["id"], precinct["label"], "Единая Россия", "Мужской", "25–34", "s1", "", tik],
    ]
    result = dashboard_snapshot(values, settings, requested_day="2026-09-16")
    counts = [item["count"] for item in result["parties"]]
    assert counts == sorted(counts, reverse=True)
    assert result["parties"][0]["label"] == "Единая Россия"


def test_new_people_by_okrug(settings):
    balashikha = next(p for p in settings.precincts if p["tik"] == "ТИК города Балашиха")
    values = [
        ["one", "2026-09-16T08:00:00+00:00", "2026-09-16", "А", "Б",
         balashikha["id"], balashikha["label"], "Новые люди", "Женский", "25–34", "s1", "", "ТИК города Балашиха"],
        ["two", "2026-09-16T08:05:00+00:00", "2026-09-16", "А", "Б",
         balashikha["id"], balashikha["label"], "Единая Россия", "Женский", "25–34", "s1", "", "ТИК города Балашиха"],
    ]
    result = dashboard_snapshot(values, settings, requested_day="2026-09-16")
    assert len(result["new_people_by_okrug"]) == 12
    entry = next(x for x in result["new_people_by_okrug"] if x["label"] == "Округ 118")
    assert entry["count"] == 1
    assert entry["percent"] == 50.0


def test_dashboard_uses_separate_code(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.access_code = "interviewer-code"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/dashboard/data").status_code == 401
        assert client.post("/api/dashboard/login", json={"code":"interviewer-code"}).status_code == 401
        assert client.post("/api/dashboard/login", json={"code":"coordinator-secret"}).status_code == 200
        assert client.get("/api/dashboard/data").status_code == 200


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

import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import re
from urllib.parse import unquote

from app.main import (ANOMALY_HEADERS, OKRUGS, Settings, TIK_TO_OKRUG, connect, create_app, dashboard_snapshot,
                      dashboard_detail, detect_anomalies, export_once, forecast_shares, parse_sheet_rows, read_anomaly_statuses, write_anomaly_status)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "poll.sqlite3"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ACCESS_CODE", "")
    monkeypatch.setenv("REFUSAL_DEMOGRAPHICS", "true")
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "")
    monkeypatch.setenv("SMS_GATEWAY_URL", "")
    monkeypatch.setenv("SMS_GATEWAY_TOKEN", "")
    monkeypatch.setattr("app.main.read_anomaly_statuses", lambda *_args, **_kwargs: {})
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


PARTY_CYCLE = ["Единая Россия", "ЛДПР", "КПРФ", "Новые люди", "Зелёные", "Родина"]
AGE_CYCLE = ["18–24", "25–34", "35–44", "45–60", "61+"]


def _pick(value, default, index):
    if value is None:
        return default(index)
    return value(index) if callable(value) else value


def shift_rows(settings, count, shift="s1", name="Иван", start="2026-09-16T07:00:00+00:00", step=180,
               answer=None, gender=None, age=None, delay=0, day="2026-09-16"):
    precinct = settings.precincts[0]
    base = datetime.fromisoformat(start)
    rows = []
    for i in range(count):
        created = base + timedelta(seconds=step * i)
        rows.append([
            f"{shift}-{i}", created.isoformat(), day, "Тестов", name, precinct["id"], precinct["label"],
            _pick(answer, lambda n: PARTY_CYCLE[n % 6], i),
            _pick(gender, lambda n: ["Мужской", "Женский"][n % 2], i),
            _pick(age, lambda n: AGE_CYCLE[n % 5], i),
            shift, (created + timedelta(seconds=delay)).isoformat(), precinct["tik"]])
    return rows


def anomaly_rules(settings, rows):
    return {item["rule"]: item for item in detect_anomalies(parse_sheet_rows(rows), settings)}


def test_anomaly_clean_shift_has_none(settings):
    assert anomaly_rules(settings, shift_rows(settings, 20)) == {}


def test_anomaly_fast_entry(settings):
    found = anomaly_rules(settings, shift_rows(settings, 8, step=5))
    assert list(found) == ["fast"] and found["fast"]["severity"] == "high"


def test_anomaly_identical_run(settings):
    rows = shift_rows(settings, 20,
                      answer=lambda i: "КПРФ" if 5 <= i < 11 else PARTY_CYCLE[i % 6],
                      gender=lambda i: "Мужской" if 5 <= i < 11 else ["Мужской", "Женский"][i % 2],
                      age=lambda i: "35–44" if 5 <= i < 11 else AGE_CYCLE[i % 5])
    found = anomaly_rules(settings, rows)
    assert found["run"]["severity"] == "medium" and "6 анкет подряд" in found["run"]["detail"]


def test_anomaly_dominant_answer(settings):
    found = anomaly_rules(settings, shift_rows(settings, 20, answer="Единая Россия"))
    assert list(found) == ["dominant"] and found["dominant"]["severity"] == "high"


def test_anomaly_uniform_respondents(settings):
    found = anomaly_rules(settings, shift_rows(settings, 20, gender="Мужской", age="25–34"))
    assert list(found) == ["uniform"]


def test_anomaly_refusal_rate_vs_daily_average(settings):
    rows = (shift_rows(settings, 20, shift="a", name="Иван", answer="Отказался отвечать")
            + shift_rows(settings, 40, shift="b", name="Пётр"))
    found = {item["interviewer"]: item for item in detect_anomalies(parse_sheet_rows(rows), settings)
             if item["rule"] == "refusals"}
    assert found["Тестов Иван"]["severity"] == "high"
    assert "Тестов Пётр" in found


def test_anomaly_outside_working_hours(settings):
    found = anomaly_rules(settings, shift_rows(settings, 4, start="2026-09-16T20:00:00+00:00"))
    assert list(found) == ["hours"] and found["hours"]["severity"] == "medium"


def test_anomaly_late_sync(settings):
    found = anomaly_rules(settings, shift_rows(settings, 3, delay=13 * 3600))
    assert list(found) == ["late"]


def test_anomaly_scope_ids_and_statuses(settings):
    rows = shift_rows(settings, 8, step=5)
    okrug = TIK_TO_OKRUG[settings.precincts[0]["tik"]]
    other = next(o for o in OKRUGS if o != okrug)
    base = dashboard_snapshot(rows, settings, requested_day="2026-09-16")["anomalies"]
    assert base["open"] == 1 and base["closed"] == 0
    item = base["items"][0]
    assert dashboard_snapshot(rows, settings, requested_day="all", requested_okrug=okrug)["anomalies"]["items"][0]["id"] == item["id"]
    assert dashboard_snapshot(rows, settings, requested_day="2026-09-15")["anomalies"]["items"] == []
    assert dashboard_snapshot(rows, settings, requested_day="all", requested_okrug=other)["anomalies"]["items"] == []
    closed = dashboard_snapshot(rows, settings, requested_day="2026-09-16",
                                statuses={item["id"]: {"status": "resolved", "note": "проверено", "updated_at": "x"}})["anomalies"]
    assert closed["open"] == 0 and closed["closed"] == 1
    assert closed["items"][0]["status"] == "resolved" and closed["items"][0]["note"] == "проверено"


def test_party_okrug_heatmap(settings):
    balashikha = next(p for p in settings.precincts if p["tik"] == "ТИК города Балашиха")
    dmitrov = next(p for p in settings.precincts if p["tik"] == "ТИК города Дмитров")

    def row(i, precinct, answer):
        return [f"r{i}", "2026-09-16T08:00:00+00:00", "2026-09-16", "А", "Б", precinct["id"], precinct["label"],
                answer, "Мужской", "25–34", "s1", "", precinct["tik"]]
    rows = [row(1, balashikha, "Единая Россия"), row(2, balashikha, "Новые люди"),
            row(3, balashikha, "Отказался отвечать"), row(4, dmitrov, "Единая Россия")]
    heat = dashboard_snapshot(rows, settings, requested_day="2026-09-16")["party_okrug"]
    assert len(heat["okrugs"]) == 12
    assert next(o for o in heat["okrugs"] if o["okrug"] == "118")["total"] == 3
    labels = [r["label"] for r in heat["rows"]]
    assert labels[0] == "Единая Россия" and labels[-2:] == ["Испортил бюллетень", "Отказался отвечать"]
    assert "Яблоко" not in labels
    cell = heat["rows"][0]["cells"][0]
    assert cell == {"count": 1, "percent": 33.3}


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code = payload or {}, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSheetsSession:
    """Just enough of the Sheets API for the anomaly tab."""

    def __init__(self):
        self.titles, self.rows, self.value_posts = set(), {}, []

    def get(self, url, timeout=None):
        if "fields=sheets.properties.title" in url:
            return FakeResponse({"sheets": [{"properties": {"title": t}} for t in sorted(self.titles)]})
        target = unquote(url.split("/values/")[1])
        if "Аномалии" not in self.titles:
            return FakeResponse(status=400)
        width = 1 if target.endswith("A2:A") else 4
        last = max(self.rows, default=1)
        return FakeResponse({"values": [(self.rows.get(n) or [])[:width] for n in range(2, last + 1)]})

    def post(self, url, json=None, timeout=None):
        if url.endswith("/values:batchUpdate"):
            self.value_posts.append(json)
            for entry in json["data"]:
                number = int(re.search(r"!A(\d+):I", entry["range"]).group(1))
                self.rows[number] = entry["values"][0]
        else:
            self.titles.add(json["requests"][0]["addSheet"]["properties"]["title"])
        return FakeResponse()


def test_anomaly_statuses_roundtrip_in_google_tab(settings):
    settings.spreadsheet = "sheet-id"
    session = FakeSheetsSession()
    assert read_anomaly_statuses(settings, session) == {}
    record = {"id": "a" * 16, "status": "clarified", "note": "Звонили", "updated_at": "2026-09-16T12:00:00+03:00",
              "interviewer": "Тестов Иван", "rule": "fast", "day": "2026-09-16", "tik": "ТИК", "precinct": "УИК № 1"}
    write_anomaly_status(settings, record, session)
    assert "Аномалии" in session.titles and session.rows[1] == ANOMALY_HEADERS
    assert read_anomaly_statuses(settings, session) == {
        "a" * 16: {"status": "clarified", "note": "Звонили", "updated_at": "2026-09-16T12:00:00+03:00"}}
    write_anomaly_status(settings, {**record, "status": "resolved", "note": '=IMPORTXML("x")'}, session)
    write_anomaly_status(settings, {**record, "id": "b" * 16}, session)
    assert sorted(session.rows) == [1, 2, 3]
    assert session.rows[2][1] == "Устранена" and session.rows[2][2].startswith("=IMPORTXML")
    assert all(post["valueInputOption"] == "RAW" for post in session.value_posts)


def test_anomaly_status_endpoint(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    rows = shift_rows(settings, 8, step=5)
    saved = []
    monkeypatch.setattr("app.main.read_sheet", lambda _: rows)
    monkeypatch.setattr("app.main.write_anomaly_status", lambda _s, record, session=None: saved.append(record))
    with TestClient(create_app(settings)) as client:
        payload = {"id": "0" * 16, "status": "resolved"}
        assert client.post("/api/dashboard/anomalies/status", json=payload).status_code == 401
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        item = client.get("/api/dashboard/data", params={"day": "2026-09-16"}).json()["anomalies"]["items"][0]
        assert client.post("/api/dashboard/anomalies/status", json=payload).status_code == 404
        assert client.post("/api/dashboard/anomalies/status", json={"id": "bad", "status": "resolved"}).status_code == 422
        ok = client.post("/api/dashboard/anomalies/status",
                         json={"id": item["id"], "status": "clarified", "note": "Звонили, всё в порядке"})
        assert ok.status_code == 200
        assert saved[0]["status"] == "clarified" and saved[0]["interviewer"] == item["interviewer"]
        anomalies = client.get("/api/dashboard/data", params={"day": "2026-09-16"}).json()["anomalies"]
        assert anomalies["open"] == 0 and anomalies["items"][0]["status"] == "clarified"


def test_anomaly_status_requires_sheet(settings):
    settings.dashboard_code = "coordinator-secret"
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        response = client.post("/api/dashboard/anomalies/status", json={"id": "0" * 16, "status": "open"})
        assert response.status_code == 503


def test_new_people_by_age(settings):
    rows = shift_rows(settings, 10,
                      answer=lambda i: "Новые люди" if i < 3 else "КПРФ",
                      age=lambda i: "25–34" if i < 4 else "61+")
    result = dashboard_snapshot(rows, settings, requested_day="2026-09-16")["new_people_by_age"]
    assert [g["label"] for g in result] == ["18–24", "25–34", "35–44", "45–60", "61+"]
    young = next(g for g in result if g["label"] == "25–34")
    old = next(g for g in result if g["label"] == "61+")
    assert (young["count"], young["total"], young["percent"]) == (3, 4, 75.0)
    assert (old["count"], old["total"], old["percent"]) == (0, 6, 0)
    assert next(g for g in result if g["label"] == "18–24") == {"label": "18–24", "count": 0, "total": 0, "percent": 0}
    other_day = dashboard_snapshot(rows, settings, requested_day="2026-09-15")["new_people_by_age"]
    assert all(g["total"] == 0 for g in other_day)


def survey_rows(settings, spec):
    """spec: list of (count, answer, gender, age); all in one UIK of the first precinct."""
    precinct = settings.precincts[0]
    rows = []
    for count, answer, gender, age in spec:
        for _ in range(count):
            rows.append([f"r{len(rows)}", "2026-09-16T08:00:00+00:00", "2026-09-16", "А", "Б", precinct["id"],
                         precinct["label"], answer, gender, age, "s1", "", precinct["tik"]])
    return rows


def forecast_of(settings, spec, uik_counts=None):
    return forecast_shares(parse_sheet_rows(survey_rows(settings, spec)), uik_counts)


def test_forecast_is_neutral_when_refusers_look_like_respondents(settings):
    forecast = forecast_of(settings, [(300, "Единая Россия", "Мужской", "25–34"), (100, "КПРФ", "Мужской", "25–34"),
                                      (150, "Отказался отвечать", "Мужской", "25–34")])
    shares = {r["label"]: r for r in forecast["rows"]}
    assert forecast["scope"]["respondents"] == 400 and forecast["scope"]["refusers"] == 150
    assert shares["Единая Россия"]["answered"] == 75.0
    assert abs(shares["Единая Россия"]["forecast"] - 75.0) < 0.6
    assert abs(sum(r["forecast"] for r in forecast["rows"]) - 100) < 0.3
    assert all(r["margin"] > 0 for r in forecast["rows"])


def test_forecast_reweights_by_who_refuses(settings):
    rows = {r["label"]: r for r in forecast_of(settings, [
        (300, "Единая Россия", "Мужской", "61+"), (300, "КПРФ", "Мужской", "18–24"),
        (300, "Отказался отвечать", "Мужской", "61+")])["rows"]}
    assert rows["Единая Россия"]["answered"] == 50.0
    assert rows["Единая Россия"]["with_refusals"] > 60 and rows["Единая Россия"]["delta"] > 10


def test_forecast_weights_territories_by_uik_count(settings):
    balashikha = next(p for p in settings.precincts if p["tik"] == "ТИК города Балашиха")
    dmitrov = next(p for p in settings.precincts if p["tik"] == "ТИК города Дмитров")

    def rows(precinct, answer, prefix):
        return [[f"{prefix}{i}", "2026-09-16T08:00:00+00:00", "2026-09-16", "А", "Б", precinct["id"], precinct["label"],
                 answer, "Мужской", "25–34", prefix, "", precinct["tik"]] for i in range(300)]
    data = parse_sheet_rows(rows(balashikha, "Единая Россия", "a") + rows(dmitrov, "КПРФ", "b"))
    even = {r["label"]: r for r in forecast_shares(data)["rows"]}
    weighted = {r["label"]: r for r in forecast_shares(data, {"ТИК города Балашиха": 3, "ТИК города Дмитров": 1})["rows"]}
    assert even["Единая Россия"]["forecast"] == 50.0
    assert weighted["Единая Россия"]["answered"] == 50.0 and weighted["Единая Россия"]["forecast"] > 65
    assert weighted["Единая Россия"]["forecast"] + weighted["КПРФ"]["forecast"] > 98


def test_forecast_history_ages_and_deg_scenarios(settings):
    spec = [(200, "Единая Россия", "Мужской", "61+"), (200, "КПРФ", "Мужской", "18–24"),
            (100, "Отказался отвечать", "Мужской", "61+")]
    forecast = forecast_of(settings, spec)
    points = forecast["history"]["points"]
    assert points[-1]["fraction"] == 1.0 and points[0]["n"] < points[-1]["n"]
    assert forecast["history"]["series"][0]["values"][-1] == forecast["rows"][0]["forecast"]
    old = next(a for a in forecast["ages"] if a["age"] == "61+")
    assert old["leader"] == "Единая Россия" and old["refusal_rate"] == 33.3
    scenarios = {s["key"]: s["values"] for s in forecast["deg"]["scenarios"]}
    assert scenarios["young"]["КПРФ"] > scenarios["older"]["КПРФ"]
    assert abs(sum(scenarios["young"].values()) - 100) < 0.5


def test_forecast_without_valid_answers_is_none(settings):
    assert forecast_of(settings, [(5, "Отказался отвечать", "Мужской", "61+")]) is None
    assert forecast_shares([]) is None


def test_snapshot_has_forecast_and_age_heatmap(settings):
    spec = [(30, "Единая Россия", "Мужской", "61+"), (10, "КПРФ", "Женский", "18–24"),
            (10, "Отказался отвечать", "Женский", "61+")]
    snap = dashboard_snapshot(survey_rows(settings, spec), settings, requested_day="2026-09-16")
    assert snap["forecast"]["rows"][0]["label"] == "Единая Россия"
    heat = snap["party_age"]
    assert [a["age"] for a in heat["ages"]] == ["18–24", "25–34", "35–44", "45–60", "61+"]
    assert heat["overall"] == 50 and next(a for a in heat["ages"] if a["age"] == "61+")["total"] == 40
    assert heat["rows"][0]["label"] == "Единая Россия" and heat["rows"][0]["cells"][4] == {"count": 30}
    assert [r["label"] for r in heat["rows"][-2:]] == ["Испортил бюллетень", "Отказался отвечать"]
    other_day = dashboard_snapshot(survey_rows(settings, spec), settings, requested_day="2026-09-15")
    assert other_day["forecast"]["rows"] == snap["forecast"]["rows"]


def detail(settings, rows, kind, key, **scope):
    return dashboard_detail(rows, settings, kind, key, scope.pop("day", "2026-09-16"), **scope)


def breakdown(payload, title):
    return next(b for b in payload["breakdowns"] if b["title"] == title)


def test_refusal_detail_shows_who_refuses(settings):
    rows = (shift_rows(settings, 60, answer=lambda i: "Отказался отвечать" if i % 2 == 0 else "КПРФ",
                       age=lambda i: "61+" if i % 2 == 0 else "25–34")
            + shift_rows(settings, 40, shift="s2", name="Пётр", answer="КПРФ", age="25–34"))
    payload = detail(settings, rows, "party", "Отказался отвечать")
    assert payload["kind"] == "party" and payload["metrics"][0]["value"] == "30"
    ages = {r["label"]: r for r in breakdown(payload, "По возрасту")["rows"]}
    assert ages["61+"]["percent"] == 100 and ages["25–34"]["percent"] == 0
    assert any("61+" in f["text"] and "чаще всего" in f["text"] for f in payload["findings"])
    people = breakdown(payload, "Интервьюеры с наибольшей долей отказов")["rows"]
    assert people[0]["label"] == "Тестов Иван" and people[0]["percent"] == 50


def test_party_detail_rank_forecast_and_small_sample(settings):
    rows = shift_rows(settings, 120)
    payload = detail(settings, rows, "party", "Единая Россия")
    text = " ".join(f["text"] for f in payload["findings"])
    assert "-е место из" in text and "Прогноз по всем данным" in text
    small = detail(settings, shift_rows(settings, 6), "party", "Единая Россия")
    assert any("мало" in f["text"] for f in small["findings"])
    assert detail(settings, shift_rows(settings, 6), "party", "Родина")["findings"][0]["text"].startswith("Таких анкет")


def test_group_detail_compares_with_whole_area(settings):
    rows = (shift_rows(settings, 80, answer="КПРФ", age="61+")
            + shift_rows(settings, 80, shift="s2", answer="Единая Россия", age="25–34"))
    payload = detail(settings, rows, "age", "61+")
    assert payload["title"] == "Возраст 61+" and payload["metrics"][0]["value"] == "80"
    parties = {r["label"]: r for r in breakdown(payload, "Партии среди назвавших партию")["rows"]}
    assert parties["КПРФ"]["percent"] == 100 and parties["КПРФ"]["baseline"] == 50
    assert any("«КПРФ» здесь 100%" in f["text"] for f in payload["findings"])
    assert not any(b["title"] == "Возрастной состав" for b in payload["breakdowns"])
    assert any(b["title"] == "Состав по полу" for b in payload["breakdowns"])


def test_okrug_hour_and_gender_details(settings):
    rows = shift_rows(settings, 60)
    okrug = TIK_TO_OKRUG[settings.precincts[0]["tik"]]
    assert detail(settings, rows, "okrug", okrug)["metrics"][0]["value"] == "60"
    assert detail(settings, rows, "okrug", "999" if "999" not in OKRUGS else "118")["title"].startswith("Округ")
    hour = detail(settings, rows, "hour", "10")
    assert hour["title"] == "Час 10:00–10:59" and int(hour["metrics"][0]["value"].replace("\u00a0", "")) > 0
    assert detail(settings, rows, "gender", "Женский")["title"] == "Женщины"
    empty = detail(settings, [], "gender", "Мужской")
    assert empty["findings"][0]["text"].startswith("Анкет в этой группе")


def test_detail_endpoint_validation_and_scope(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    rows = shift_rows(settings, 60)
    monkeypatch.setattr("app.main.read_sheet", lambda _: rows)
    with TestClient(create_app(settings)) as client:
        query = {"kind": "party", "key": "КПРФ", "day": "2026-09-16"}
        assert client.get("/api/dashboard/detail", params=query).status_code == 401
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        assert client.get("/api/dashboard/detail", params=query).status_code == 200
        assert client.get("/api/dashboard/detail", params={**query, "kind": "nope"}).status_code == 422
        assert client.get("/api/dashboard/detail", params={**query, "key": "Яблоко"}).status_code == 422
        assert client.get("/api/dashboard/detail", params={**query, "kind": "age", "key": "17"}).status_code == 422
        assert client.get("/api/dashboard/detail", params={**query, "okrug": "999"}).status_code == 422
        body = client.get("/api/dashboard/detail", params={**query, "day": "2026-09-15"}).json()
        assert body["metrics"][0]["value"] == "0"

import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import re
from urllib.parse import unquote

from app.main import (ANOMALY_HEADERS, OKRUGS, Settings, TIK_TO_OKRUG, connect, create_app, dashboard_snapshot,
                      BASELINE, baseline_shares, build_swing, dashboard_detail, detect_anomalies, export_once, forecast_shares, parse_sheet_rows, read_anomaly_statuses, territory_weights, write_anomaly_status)


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


FOCUS = "Новые люди"


def detail(settings, rows, kind, key, focus=FOCUS, day="2026-09-16"):
    return dashboard_detail(rows, settings, kind, key, focus=focus, requested_day=day)


def mk_rows(settings, spec):
    """spec: (tik, count, answer, gender, age, shift)"""
    rows = []
    for tik, count, answer, gender, age, shift in spec:
        precinct = next(p for p in settings.precincts if p["tik"] == tik)
        for _ in range(count):
            n = len(rows)
            rows.append([f"m{n}", f"2026-09-16T{6 + n % 10:02d}:{n % 60:02d}:00+00:00", "2026-09-16", "А", shift, precinct["id"],
                         precinct["label"], answer, gender, age, shift, "", tik])
    return rows


def texts(payload, section):
    return " ".join(f["text"] for sec in payload["sections"] if sec["title"] == section for f in sec["findings"])


BAL, DMI, KHI = "ТИК города Балашиха", "ТИК города Дмитров", "ТИК города Химки"


def test_focus_profile_finds_core_weak_spots_and_significance(settings):
    rows = mk_rows(settings, [(BAL, 80, FOCUS, "Мужской", "25–34", "a"), (BAL, 120, "КПРФ", "Мужской", "25–34", "b"),
                              (BAL, 10, FOCUS, "Мужской", "61+", "c"), (BAL, 190, "КПРФ", "Мужской", "61+", "d")])
    payload = detail(settings, rows, "party", FOCUS)
    assert payload["role"] == "Ваша партия" and payload["focus"] == FOCUS
    seen = texts(payload, "Что видно")
    assert "Ядро — 25–34" in seen and "Слабое место — 61+" in seen
    assert payload["headline"]["text"].startswith("«Новые люди»: 22.5%")
    ages = next(t for sec in payload["sections"] if sec["title"] == "Сегменты поддержки" for t in sec["tables"] if "возрасту" in t["title"])
    young = next(r for r in ages["rows"] if r[0]["t"] == "25–34")
    assert young[3]["t"] == "178" and young[6]["t"] == "значимо выше"


def test_group_analysis_separates_composition_from_real_effect(settings):
    explained = mk_rows(settings, [
        (BAL, 120, FOCUS, "Мужской", "25–34", "a"), (BAL, 180, "КПРФ", "Мужской", "25–34", "b"),
        (BAL, 5, FOCUS, "Мужской", "61+", "c"), (BAL, 95, "КПРФ", "Мужской", "61+", "d"),
        (DMI, 40, FOCUS, "Мужской", "25–34", "e"), (DMI, 60, "КПРФ", "Мужской", "25–34", "f"),
        (DMI, 15, FOCUS, "Мужской", "61+", "g"), (DMI, 285, "КПРФ", "Мужской", "61+", "h")])
    assert "объясняется составом" in texts(detail(settings, explained, "okrug", "118"), "Позиция «Новые люди»")
    real = mk_rows(settings, [
        (BAL, 250, FOCUS, "Мужской", "25–34", "a"), (BAL, 50, "КПРФ", "Мужской", "25–34", "b"),
        (BAL, 5, FOCUS, "Мужской", "61+", "c"), (BAL, 95, "КПРФ", "Мужской", "61+", "d"),
        (DMI, 20, FOCUS, "Мужской", "25–34", "e"), (DMI, 80, "КПРФ", "Мужской", "25–34", "f"),
        (DMI, 15, FOCUS, "Мужской", "61+", "g"), (DMI, 285, "КПРФ", "Мужской", "61+", "h")])
    assert "свойство самой группы" in texts(detail(settings, real, "okrug", "118"), "Позиция «Новые люди»")


def test_rival_view_shows_where_we_lose_and_win(settings):
    rows = mk_rows(settings, [(BAL, 100, "КПРФ", "Мужской", "61+", "a"), (BAL, 20, FOCUS, "Мужской", "61+", "b"),
                              (BAL, 100, FOCUS, "Мужской", "25–34", "c"), (BAL, 20, "КПРФ", "Мужской", "25–34", "d")])
    payload = detail(settings, rows, "party", "КПРФ")
    seen = texts(payload, "Что видно")
    assert payload["role"] == "Конкурент"
    assert "Уступаем значимо: 61+" in seen and "Опережаем значимо: 25–34" in seen
    assert any(sec["title"] == "Кто голосует за «КПРФ»" for sec in payload["sections"])


def test_refusal_view_profiles_refusers_and_links_reserve_to_focus(settings):
    rows = mk_rows(settings, [
        (BAL, 60, "Отказался отвечать", "Мужской", "61+", "a"), (BAL, 40, FOCUS, "Мужской", "61+", "a"),
        (BAL, 10, "Отказался отвечать", "Мужской", "25–34", "b"), (BAL, 90, FOCUS, "Мужской", "25–34", "b"),
        (BAL, 20, "Отказался отвечать", "Мужской", "35–44", "c"), (BAL, 80, "КПРФ", "Мужской", "35–44", "c")])
    payload = detail(settings, rows, "party", "Отказался отвечать")
    titles = [sec["title"] for sec in payload["sections"]]
    assert payload["role"].startswith("Отказы") and "Интервьюеры" in titles and any("Что скрывают отказы" in t for t in titles)
    assert "61+" in texts(payload, "Что видно") and "значимо чаще" in texts(payload, "Что видно")
    spoiled = detail(settings, rows, "party", "Испортил бюллетень")
    assert spoiled["headline"]["text"].endswith("нет.")


def test_shift_concentration_is_flagged(settings):
    spec = [(BAL, 40, FOCUS, "Мужской", "25–34", f"s{i}") for i in range(3)]
    spec += [(BAL, 40, "КПРФ", "Мужской", "25–34", f"t{i}") for i in range(5)]
    assert "Три смены" in texts(detail(settings, mk_rows(settings, spec), "party", FOCUS), "Что видно")


def test_focus_can_be_switched_and_edge_cases_do_not_crash(settings):
    rows = mk_rows(settings, [(BAL, 60, "КПРФ", "Мужской", "25–34", "a"), (BAL, 40, FOCUS, "Женский", "45–60", "b")])
    assert detail(settings, rows, "party", "КПРФ", focus="КПРФ")["role"] == "Ваша партия"
    for kind, key in (("party", "Родина"), ("gender", "Женский"), ("age", "45–60"), ("okrug", "118"), ("hour", "10"), ("party", "Испортил бюллетень")):
        for data in (rows, [], mk_rows(settings, [(BAL, 3, "КПРФ", "Мужской", "25–34", "a")])):
            payload = detail(settings, data, kind, key)
            assert payload["title"] and payload["headline"]["text"] and isinstance(payload["sections"], list)


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
        assert client.get("/api/dashboard/detail", params={**query, "focus": "Отказался отвечать"}).status_code == 422
        assert client.get("/api/dashboard/detail", params={**query, "focus": "Яблоко"}).status_code == 422
        assert client.get("/api/dashboard/detail", params={**query, "focus": "КПРФ"}).json()["focus"] == "КПРФ"
        body = client.get("/api/dashboard/detail", params={**query, "day": "2026-09-15"}).json()
        assert body["metrics"][0]["value"] == "0"


def test_baseline_2021_is_loaded_and_renormalised():
    assert BASELINE and len(BASELINE["okrugs"]) == 12
    region = baseline_shares()
    assert abs(sum(region.values()) - 100) < 0.5
    assert "Партия прямой демократии" not in region and "Яблоко" not in region
    assert region["Единая Россия"] > region["КПРФ"] > region["Новые люди"]
    assert baseline_shares("123")["Новые люди"] > baseline_shares("128")["Новые люди"]  # 6.6% against 5.4% on the 2026 boundaries
    assert baseline_shares("123")["Единая Россия"] < 40 < baseline_shares("121")["Единая Россия"]  # Mytishchi/Korolev vs Istra/Krasnogorsk


def test_territory_weights_follow_okrug_electorate(settings):
    weights = territory_weights(settings)
    assert len(weights) == 56 and abs(sum(weights.values()) - 1) < 1e-9
    voters = {okrug: entry["voters"] for okrug, entry in BASELINE["okrugs"].items()}
    total = sum(voters.values())
    for okrug, tiks in OKRUGS.items():
        assert abs(sum(weights[t] for t in tiks) - voters[okrug] / total) < 1e-9


def test_forecast_uses_weights_and_reports_2021_and_flow(settings):
    rows = shift_rows(settings, 60, start="2026-09-18T09:00:00+00:00", step=600, day="2026-09-18")
    forecast = forecast_shares(parse_sheet_rows(rows), territory_weights(settings))
    first = forecast["rows"][0]
    assert first["y2021"] == baseline_shares()[first["label"]]
    assert next(r for r in forecast["rows"] if r["label"] == "Партия прямой демократии")["y2021"] is None
    flow = forecast["flow"]
    assert flow["day"] == "2026-09-18" and flow["voted_by_share"] == 73.9 and flow["evening_share"] == 26.1
    assert flow["anket_by_as_of"] == 60 - sum(1 for r in rows if r[1] >= "2026-09-18T15:00:00+00:00")
    assert flow["sample_fraction"] == round(flow["anket_by_as_of"] * 100 / 730820, 2)
    assert forecast_shares(parse_sheet_rows(shift_rows(settings, 60)), territory_weights(settings))["flow"] is None


def test_swing_compares_poll_with_2021_by_okrug(settings):
    rows = mk_rows(settings, [(BAL, 50, FOCUS, "Мужской", "25–34", "a"), (BAL, 50, "Единая Россия", "Мужской", "25–34", "b"),
                              (DMI, 5, FOCUS, "Мужской", "25–34", "c")])
    snap = dashboard_snapshot(rows, settings, requested_day="2026-09-16")
    swing = snap["swing"]
    assert swing["year"] == 2021 and len(swing["okrugs"]) == 12
    row = next(r for r in swing["rows"] if r["label"] == FOCUS)
    index = sorted(OKRUGS).index(TIK_TO_OKRUG[BAL])
    cell = row["cells"][index]
    assert cell["poll"] == 50.0 and cell["delta"] == round(50 - cell["y2021"], 1) and cell["significant"] is True
    small = row["cells"][sorted(OKRUGS).index(TIK_TO_OKRUG[DMI])]
    assert small["n"] == 5 and small["delta"] is None
    assert swing["rows"][0]["y2021"] >= swing["rows"][1]["y2021"]


def test_okrug_analysis_has_2021_comparison(settings):
    rows = mk_rows(settings, [(BAL, 60, FOCUS, "Мужской", "25–34", "a"), (BAL, 60, "Единая Россия", "Мужской", "45–60", "b")])
    payload = detail(settings, rows, "okrug", TIK_TO_OKRUG[BAL])
    section = next(sec for sec in payload["sections"] if sec["title"] == "К итогам 2021")
    assert any(r[0]["t"] == FOCUS and r[4]["t"] == "значимо выше" for r in section["tables"][0]["rows"])
    assert "К 2021 году" in texts(payload, "Позиция «Новые люди»")
    assert not any(sec["title"] == "К итогам 2021" for sec in detail(settings, rows, "age", "25–34")["sections"])


def test_tik_map_boundaries_cover_every_tik():
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "app" / "static" / "tik_map.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_size < 400_000
    features = data["features"]
    assert sorted(f["tik"] for f in features) == sorted(TIK_TO_OKRUG)
    assert all(f["okrug"] == TIK_TO_OKRUG[f["tik"]] and f["polygons"] for f in features)
    min_x, min_y, max_x, max_y = data["bbox"]
    assert 34.5 < min_x < max_x < 40.5 and 54.0 < min_y < max_y < 57.0
    assert "OpenStreetMap" in data["attribution"]


def test_map_payload_counts_match_day_rows(settings):
    rows = mk_rows(settings, [(BAL, 40, FOCUS, "Мужской", "25–34", "a"), (BAL, 10, "Отказался отвечать", "Мужской", "25–34", "b"),
                              (DMI, 5, "Единая Россия", "Женский", "45–60", "c")])
    payload = dashboard_snapshot(rows, settings, requested_day="2026-09-16")["map"]
    assert len(payload["tiks"]) == len(TIK_TO_OKRUG) and len(payload["turnout_2021"]) == 12
    bal = next(t for t in payload["tiks"] if t["tik"] == BAL)
    assert bal["n"] == 50 and bal["answers"] == {FOCUS: 40, "Отказался отвечать": 10}
    assert sum(t["n"] for t in payload["tiks"]) == 55
    empty = dashboard_snapshot([], settings)["map"]
    assert all(t["n"] == 0 and t["answers"] == {} for t in empty["tiks"])


def roster_values(settings, spec):
    """spec: (day, tik, surname, name, count)"""
    rows = []
    for day, tik, surname, name, count in spec:
        precinct = next(p for p in settings.precincts if p["tik"] == tik)
        for i in range(count):
            rows.append([f"{day}-{surname}-{name}-{i}", f"{day}T09:{i % 60:02d}:00+03:00", day, surname, name, precinct["id"], precinct["label"],
                         "Единая Россия", "Мужской", "25–34", f"{day}-{surname}", f"{day}T09:{i % 60:02d}:00+03:00", tik])
    return rows


def test_roster_lists_interviewers_missing_today_by_okrug(settings):
    from app.main import build_roster
    now = datetime(2026, 9, 19, 11, 0, tzinfo=settings.zone)
    values = roster_values(settings, [("2026-09-18", BAL, "Иванова", "Анна", 5), ("2026-09-19", BAL, "Иванова", "Анна", 2),
                                      ("2026-09-18", BAL, "Петров", "Олег", 4), ("2026-09-18", DMI, "Шадура", "Матвей", 3),
                                      ("2026-09-19", DMI, "Матвей", "Шадура", 1), ("2026-09-19", BAL, "Новиков", "Иван", 1),
                                      ("2026-09-17", BAL, "Старый", "Давно", 9)])
    roster = build_roster(values, settings, now)
    assert roster["today"] == "2026-09-19" and roster["yesterday"] == "2026-09-18" and roster["days"] == ["2026-09-17", "2026-09-18", "2026-09-19"]
    assert {k: roster["totals"][k] for k in ("all", "yesterday", "today", "absent", "new", "both")} == {"all": 5, "yesterday": 3, "today": 3, "absent": 2, "new": 1, "both": 2}
    people = {p["name"]: p for okrug in roster["okrugs"] for p in okrug["people"]}
    assert people["Петров Олег"]["status"] == "absent" and people["Петров Олег"]["yesterday"] == 4
    assert people["Иванова Анна"]["status"] == "both" and people["Новиков Иван"]["status"] == "new"
    assert people["Шадура Матвей"]["status"] == "both"
    old = people["Старый Давно"]  # worked only two days ago: still counted, and missing today
    assert old["status"] == "absent" and old["by_day"] == {"2026-09-17": 9} and old["last_day"] == "2026-09-17" and old["last_day_count"] == 9
    assert people["Иванова Анна"]["by_day"] == {"2026-09-18": 5, "2026-09-19": 2} and people["Иванова Анна"]["total"] == 7
    okrug = next(o for o in roster["okrugs"] if o["okrug"] == TIK_TO_OKRUG[BAL])
    assert okrug["absent"] == 2 and okrug["all"] == 4 and [p["name"] for p in okrug["people"][:2]] == ["Старый Давно", "Петров Олег"]


def test_roster_flags_replacement_on_same_precinct(settings):
    from app.main import build_roster
    now = datetime(2026, 9, 19, 11, 0, tzinfo=settings.zone)
    values = roster_values(settings, [("2026-09-18", BAL, "Петров", "Олег", 4), ("2026-09-19", BAL, "Замена", "Ольга", 1)])
    person = next(p for o in build_roster(values, settings, now)["okrugs"] for p in o["people"] if p["name"] == "Петров Олег")
    assert person["replaced_by"] == ["Замена Ольга"]


def test_roster_needs_dashboard_and_roster_passwords(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/dashboard/roster").status_code == 401
        assert client.post("/api/dashboard/roster/login", json={"code": "exitpoll"}).status_code == 401  # no coordinator session yet
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        assert client.get("/api/dashboard/roster").status_code == 401  # coordinator code alone is not enough
        assert client.post("/api/dashboard/roster/login", json={"code": "wrong"}).status_code == 401
        assert client.post("/api/dashboard/roster/login", json={"code": "coordinator-secret"}).status_code == 401
        assert client.post("/api/dashboard/roster/login", json={"code": "exitpoll"}).status_code == 200
        response = client.get("/api/dashboard/roster")
        assert response.status_code == 200 and response.json()["totals"]["absent"] == 0
    with TestClient(create_app(settings)) as other:  # a fresh browser has neither session
        assert other.get("/api/dashboard/roster").status_code == 401


def test_roster_code_env_override_and_no_plaintext_in_source(settings):
    from pathlib import Path
    from app.main import roster_code_ok
    assert roster_code_ok("exitpoll") and not roster_code_ok("exitpoll ")
    assert roster_code_ok("другой", override="другой") and not roster_code_ok("exitpoll", override="другой")
    assert "exitpoll" not in (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")


def test_dashboard_assets_revalidate_and_roster_is_not_a_native_form(settings):
    from pathlib import Path
    with TestClient(create_app(settings)) as client:
        assert client.get("/static/dashboard.js").headers["cache-control"] == "no-cache"
        assert "cache-control" not in client.get("/static/style.css").headers or client.get("/static/style.css").headers["cache-control"] != "no-cache"
    html = (Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert '<form id="roster-login"' not in html  # an old cached script must never let the password field reload the page


def test_dashboard_script_starts_with_strict_mode_and_wires_roster_button():
    from pathlib import Path
    script = (Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert script.startswith("'use strict';")
    assert script.count("$('#roster-open').addEventListener('click', openRoster);") == 1
    assert "#roster-login').addEventListener" not in script


def test_roster_activity_from_last_anketa(settings):
    from app.main import build_roster
    day = "2026-09-19"
    precinct = next(p for p in settings.precincts if p["tik"] == BAL)

    def rows(surname, times):
        return [[f"{surname}{i}", f"{day}T{t}:00+03:00", day, surname, "И", precinct["id"], precinct["label"], "Единая Россия", "Мужской",
                 "25–34", surname, f"{day}T{t}:00+03:00", BAL] for i, t in enumerate(times)]
    values = rows("Активный", ["12:50", "13:50"]) + rows("Пауза", ["13:00"]) + rows("Молчит", ["11:00", "11:30"])
    roster = build_roster(values, settings, datetime(2026, 9, 19, 14, 0, tzinfo=settings.zone))
    people = {p["name"]: p for o in roster["okrugs"] for p in o["people"]}
    assert (people["Активный И"]["activity"], people["Активный И"]["minutes_since"], people["Активный И"]["last_today"]) == ("active", 10, "13:50")
    assert (people["Пауза И"]["activity"], people["Пауза И"]["minutes_since"]) == ("pause", 60)
    assert (people["Молчит И"]["activity"], people["Молчит И"]["minutes_since"]) == ("silent", 150)
    assert (roster["totals"]["active"], roster["totals"]["pause"], roster["totals"]["silent"]) == (1, 1, 1)
    okrug = next(o for o in roster["okrugs"] if o["okrug"] == TIK_TO_OKRUG[BAL])
    assert [p["name"] for p in okrug["people"]] == ["Молчит И", "Пауза И", "Активный И"] and okrug["silent"] == 1
    late = build_roster(values, settings, datetime(2026, 9, 19, 20, 30, tzinfo=settings.zone))
    assert {p["activity"] for o in late["okrugs"] for p in o["people"]} == {"done"}


def make_counting_sheet(monkeypatch, values=()):
    calls = {"n": 0}

    def fake(_settings):
        calls["n"] += 1
        return list(values)
    monkeypatch.setattr("app.main.read_sheet", fake)
    return calls


def test_dashboard_requests_do_not_read_the_sheet(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    settings.dashboard_refresh_seconds = 3600
    calls = make_counting_sheet(monkeypatch)
    monkeypatch.setattr("app.main.read_anomaly_statuses", lambda _s, session=None: {})
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        client.post("/api/dashboard/roster/login", json={"code": "exitpoll"})
        first = client.get("/api/dashboard/data")
        assert first.status_code == 200 and first.json()["data_age"] is not None
        baseline = calls["n"]
        for _ in range(5):
            assert client.get("/api/dashboard/data").status_code == 200
            assert client.get("/api/dashboard/data", params={"okrug": "118"}).status_code == 200
            assert client.get("/api/dashboard/roster").status_code == 200
        assert calls["n"] == baseline  # one background read serves every request


def test_background_refresh_keeps_previous_snapshot_when_sheets_fails(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    settings.dashboard_refresh_seconds = 3600
    state = {"fail": False}
    rows = mk_rows(settings, [(BAL, 3, FOCUS, "Мужской", "25–34", "a")])

    def fake(_settings):
        if state["fail"]:
            raise RuntimeError("Sheets down")
        return rows
    monkeypatch.setattr("app.main.read_sheet", fake)
    monkeypatch.setattr("app.main.read_anomaly_statuses", lambda _s, session=None: {})
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        assert client.get("/api/dashboard/data", params={"day": "2026-09-16"}).json()["summary"]["total"] == 3
        state["fail"] = True
        assert client.get("/api/dashboard/data", params={"day": "2026-09-16"}).json()["summary"]["total"] == 3  # still served


def test_health_is_light_and_needs_no_sheet(settings, monkeypatch):
    def boom(_settings):
        raise AssertionError("health must not touch the sheet")
    monkeypatch.setattr("app.main.read_sheet", boom)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json() == {"ok": True}


def test_interviewer_count_is_unique_people_across_days(settings):
    precinct = next(p for p in settings.precincts if p["tik"] == BAL)
    rows = []
    for day, surname, name, shift in [("2026-09-18", "Чернец", "Аня", "s18"), ("2026-09-19", "Аня", "Чернец", "s19"),
                                       ("2026-09-19", "Петров", "Олег", "s19b")]:
        rows.append([f"{shift}-1", f"{day}T09:00:00+03:00", day, surname, name, precinct["id"], precinct["label"], "Единая Россия",
                     "Мужской", "25–34", shift, f"{day}T09:00:00+03:00", BAL])
    everything = dashboard_snapshot(rows, settings, requested_day="all")
    assert everything["summary"]["interviewers"] == 2  # three shifts, two people
    assert next(o for o in everything["okrug_stats"] if o["okrug"] == TIK_TO_OKRUG[BAL])["interviewers"] == 2
    assert dashboard_snapshot(rows, settings, requested_day="2026-09-19")["summary"]["interviewers"] == 2
    assert dashboard_snapshot(rows, settings, requested_day="2026-09-18")["summary"]["interviewers"] == 1


def test_every_answer_has_a_chart_colour():
    from pathlib import Path
    from app.main import PARTIES
    script = (Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")
    block = script[script.index("const PARTY_COLORS"):script.index("const GENDER_COLORS")]
    assert all(f"'{label}'" in block for _, label in PARTIES)


def test_every_party_logo_referenced_by_the_dashboard_exists():
    import re
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "app" / "static"
    script = (static / "dashboard.js").read_text(encoding="utf-8")
    block = script[script.index("const PARTY_LOGOS"):script.index("let lastParties")]
    files = re.findall(r":\s*'([a-z-]+)'", block)
    assert len(files) == 11 and all((static / "logos" / f"{name}.png").stat().st_size > 1000 for name in files)
    assert sum(f.stat().st_size for f in (static / "logos").iterdir()) < 500_000


def kpi_values(settings, spec):
    """spec: (day, HH:MM, tik, answer, surname)"""
    rows = []
    for i, (day, clock, tik, answer, surname) in enumerate(spec):
        precinct = next(p for p in settings.precincts if p["tik"] == tik)
        stamp = f"{day}T{clock}:00+03:00"
        rows.append([f"k{i}", stamp, day, surname, "И", precinct["id"], precinct["label"], answer, "Мужской", "25–34", f"s{day}{surname}", stamp, tik])
    return rows


def test_kpi_compare_uses_the_same_clock_time_for_the_running_day(settings):
    spec = ([("2026-09-18", "09:00", BAL, "Единая Россия", "А")] * 4 + [("2026-09-18", "15:00", BAL, "Отказался отвечать", "Б")] * 6
            + [("2026-09-19", "09:30", BAL, "Единая Россия", "А")] * 3 + [("2026-09-19", "10:00", BAL, "Отказался отвечать", "В")] * 2)
    compare = dashboard_snapshot(kpi_values(settings, spec), settings)["compare"]["prev"]
    assert compare["day"] == "2026-09-19" and compare["days"] == 1 and compare["cutoff"] == "10:00"
    total = compare["values"]["total"]
    assert (total["today"], total["before"], total["change"]) == (5, 4, 25.0)  # yesterday's afternoon rows are not counted
    assert compare["values"]["refusals"]["before"] == 0 and compare["values"]["refusals"]["change"] is None
    assert compare["values"]["interviewers"] == {"today": 2, "before": 1, "change": 100.0}
    finished = dashboard_snapshot(kpi_values(settings, spec), settings, requested_day="2026-09-18")["compare"]["prev"]
    assert finished["has_previous"] is False and finished["values"]["total"]["before"] is None


def test_kpi_detail_explains_the_change(settings):
    spec = ([("2026-09-18", "09:00", BAL, "Единая Россия", "А")] * 10 + [("2026-09-19", "09:30", BAL, "Единая Россия", "А")] * 5)
    values = kpi_values(settings, spec)
    payload = dashboard_detail(values, settings, "kpi", "total")
    assert payload["role"] == "Сравнение с прошлым днём" and "на 50% меньше" in payload["headline"]["text"]
    assert [m["value"] for m in payload["metrics"][:2]] == ["5", "10"] and payload["metrics"][2]["value"] == "-5 (-50%)"
    hours = payload["sections"][0]["tables"][1]["rows"]
    assert hours[0][0]["t"] == "до 09:00" and hours[1][3]["t"] == "-5"  # 09:30 today against 09:00 yesterday: 5 vs 10 by the end of hour 9
    okrug = next(r for r in payload["sections"][1]["tables"][0]["rows"] if r[0]["t"] == f"Округ {TIK_TO_OKRUG[BAL]}")
    assert okrug[1]["t"] == "5" and okrug[2]["t"] == "10"
    assert dashboard_detail(values, settings, "kpi", "uiks", requested_day="2026-09-18")["headline"]["text"].startswith("За 17.09 данных")
    assert "сравнивать не с чем" in dashboard_detail([], settings, "kpi", "total")["headline"]["text"]


def test_kpi_detail_endpoint_validates_key(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    monkeypatch.setattr("app.main.read_anomaly_statuses", lambda _s, session=None: {})
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        assert client.get("/api/dashboard/detail", params={"kind": "kpi", "key": "total"}).status_code == 200
        assert client.get("/api/dashboard/detail", params={"kind": "kpi", "key": "nonsense"}).status_code == 422


def batch_of(settings, count, **override):
    now = datetime.now(timezone.utc)
    profile = {"id": str(uuid4()), "surname": "Пачка", "name": "Тест", "precinct": settings.precincts[0]["id"],
               "day": now.astimezone(settings.zone).date().isoformat()}
    return [{"id": str(uuid4()), "created_at": now.isoformat(), "profile": profile, "party": "11", "gender": "female", "age": "25–34", **override}
            for _ in range(count)]


def test_batch_accepts_many_surveys_and_is_idempotent(client, settings):
    items = batch_of(settings, 50)
    first = client.post("/api/surveys/batch", json={"surveys": items})
    assert first.status_code == 200 and first.json()["saved"] == 50 and all(r["saved"] for r in first.json()["results"])
    again = client.post("/api/surveys/batch", json={"surveys": items})
    assert again.status_code == 200 and again.json()["saved"] == 50
    with connect(settings) as db:
        assert db.execute("SELECT COUNT(*) FROM surveys").fetchone()[0] == 50  # no duplicates


def test_batch_gives_every_item_its_own_verdict(client, settings):
    good, wrong_precinct, bad_party, conflict = batch_of(settings, 4)
    wrong_precinct["profile"] = {**wrong_precinct["profile"], "precinct": "no-such-uik"}
    bad_party["party"] = "999"
    client.post("/api/surveys", json=conflict)
    conflict = {**conflict, "party": "1"}  # same id, different answers
    response = client.post("/api/surveys/batch", json={"surveys": [good, wrong_precinct, bad_party, conflict, {"nonsense": True}]})
    results = response.json()["results"]
    assert response.status_code == 200 and response.json()["saved"] == 1
    assert [r["saved"] for r in results] == [True, False, False, False, False]
    assert [r.get("status") for r in results[1:]] == [422, 422, 409, 422]
    assert results[1]["error"] == "Неизвестный УИК" and results[4]["id"] is None


def test_batch_limits_and_auth(settings, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "interviewer-secret-code")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    secured = Settings()
    with TestClient(create_app(secured)) as client:
        assert client.post("/api/surveys/batch", json={"surveys": batch_of(secured, 1)}).status_code == 401
        client.post("/api/login", json={"code": "interviewer-secret-code"})
        assert client.post("/api/surveys/batch", json={"surveys": []}).status_code == 422
        assert client.post("/api/surveys/batch", json={"surveys": batch_of(secured, 51)}).status_code == 422
        assert client.post("/api/surveys/batch", json={"surveys": batch_of(secured, 3)}).json()["saved"] == 3


def test_dashboard_refreshes_every_minute_by_default(settings):
    assert settings.dashboard_refresh_seconds == 60


def test_kpi_average_of_two_previous_days(settings):
    spec = ([("2026-09-17", "09:00", BAL, "Единая Россия", "А")] * 6
            + [("2026-09-18", "09:00", BAL, "Единая Россия", "А")] * 10 + [("2026-09-18", "15:00", BAL, "Единая Россия", "А")] * 4
            + [("2026-09-19", "09:30", BAL, "Единая Россия", "А")] * 12)
    values = kpi_values(settings, spec)
    compare = dashboard_snapshot(values, settings)["compare"]
    assert compare["avg2"]["days"] == 2 and compare["prev"]["days"] == 1
    assert compare["avg2"]["values"]["total"] == {"today": 12, "before": 8.0, "change": 50.0}  # (6 + 10) / 2 at 09:30
    assert compare["prev"]["values"]["total"]["before"] == 10.0
    payload = dashboard_detail(values, settings, "kpi", "total", base="avg2")
    assert payload["role"] == "Сравнение со средним за 2 дня" and payload["subtitle"].startswith("19.09 против 17.09–18.09")
    assert "12 анкет против 8 в среднем за 2 дня: на 50% больше." in payload["headline"]["text"]
    labels = {m["label"]: m["value"] for m in payload["metrics"]}
    assert labels["Среднее за 2 дня к 09:30"] == "8" and labels["Среднее за 2 дня за весь день"] == "10"
    days = payload["sections"][0]["tables"][0]["rows"]
    assert [r[0]["t"] for r in days] == ["19.09 (сегодня)", "18.09", "17.09"]
    only_one = dashboard_detail(kpi_values(settings, spec[6:]), settings, "kpi", "total", base="avg2")
    assert "второго дня нет" in only_one["headline"]["text"]
    odd = ([("2026-09-17", "09:00", BAL, "Единая Россия", "А")] * 5 + [("2026-09-18", "09:00", BAL, "Единая Россия", "А")] * 2
           + [("2026-09-19", "09:30", BAL, "Единая Россия", "А")] * 7)
    fractional = dashboard_detail(kpi_values(settings, odd), settings, "kpi", "total", base="avg2")
    assert "7 анкет против 3.5 в среднем за 2 дня: на 100% больше." in fractional["headline"]["text"]


def test_kpi_base_is_validated(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    monkeypatch.setattr("app.main.read_anomaly_statuses", lambda _s, session=None: {})
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        assert client.get("/api/dashboard/detail", params={"kind": "kpi", "key": "total", "base": "avg2"}).status_code == 200
        assert client.get("/api/dashboard/detail", params={"kind": "kpi", "key": "total", "base": "bogus"}).status_code == 422


def test_forecast_for_the_selected_day_only(settings):
    rows = mk_rows(settings, [(BAL, 40, FOCUS, "Мужской", "25–34", "a"), (BAL, 40, "Единая Россия", "Мужской", "25–34", "b")])
    day = "2026-09-16"
    other = [list(r) for r in rows]
    for r in other:
        r[2] = "2026-09-17"
        r[1] = r[1].replace(day, "2026-09-17")
        r[7] = "Единая Россия"
        r[0] = r[0] + "-x"
    both = dashboard_snapshot(rows + other, settings)
    assert both["forecast_day"]["day"] == "2026-09-17"  # the latest day when nothing is selected
    assert both["forecast_day"]["forecast"]["scope"]["days"] == ["2026-09-17"]
    assert both["forecast"]["scope"]["days"] == [day, "2026-09-17"]
    first = dashboard_snapshot(rows + other, settings, requested_day=day)["forecast_day"]
    assert first["day"] == day and first["forecast"]["scope"]["anket"] == 80
    nl = next(r for r in first["forecast"]["rows"] if r["label"] == FOCUS)["forecast"]
    nl_all = next(r for r in both["forecast"]["rows"] if r["label"] == FOCUS)["forecast"]
    assert nl > nl_all  # that day alone is friendlier to the focus party than the two days together
    assert dashboard_snapshot([], settings)["forecast_day"]["forecast"] is None


def test_party_compare_gives_other_days_and_their_average(settings):
    spec = ([("2026-09-17", "09:00", BAL, "Единая Россия", "А")] * 6 + [("2026-09-17", "09:10", BAL, "Отказался отвечать", "А")] * 4
            + [("2026-09-18", "09:00", BAL, "Единая Россия", "А")] * 5 + [("2026-09-18", "09:10", BAL, "Отказался отвечать", "А")] * 5
            + [("2026-09-19", "09:00", BAL, "Единая Россия", "А")] * 3 + [("2026-09-19", "09:10", DMI, "Единая Россия", "Б")] * 1)
    values = kpi_values(settings, spec)
    compare = dashboard_snapshot(values, settings)["party_compare"]
    assert compare["selected"] == "2026-09-19" and [d["day"] for d in compare["days"]] == ["2026-09-18", "2026-09-17"]
    assert compare["days"][0]["shares"]["Единая Россия"] == 50.0 and compare["days"][1]["shares"]["Единая Россия"] == 60.0
    assert compare["average"] == {"days": 2, "shares": {**compare["average"]["shares"], "Единая Россия": 55.0}}
    assert compare["average"]["shares"]["Отказался отвечать"] == 45.0
    every = dashboard_snapshot(values, settings, requested_day="all")["party_compare"]
    assert [d["day"] for d in every["days"]] == ["2026-09-19", "2026-09-18", "2026-09-17"]  # all days are broken down
    scoped = dashboard_snapshot(values, settings, requested_okrug=TIK_TO_OKRUG[DMI])["party_compare"]
    assert scoped["days"] == [] or all(d["total"] for d in scoped["days"])
    assert dashboard_snapshot([], settings)["party_compare"] == {"selected": dashboard_snapshot([], settings)["selected_day"], "days": [], "average": None}


def test_baseline_uses_the_2026_okrug_map_not_the_2021_numbers():
    """The 2021 map had 11 okrugs (117-127); the 2026 map has 12 (118-129) with other boundaries."""
    old = BASELINE["old_okrugs"]
    assert sorted(old) == [str(n) for n in range(117, 128)] and sorted(BASELINE["okrugs"]) == [str(n) for n in range(118, 130)]
    total_old = sum(e["voters"] for e in old.values())
    total_new = sum(e["voters"] for e in BASELINE["okrugs"].values())
    assert abs(total_new - total_old) / total_old < 0.03  # the split only moves electors between okrugs
    for old_id in old:  # every 2021 okrug is fully distributed over the new ones
        assert 95 <= sum(part.get(old_id, 0) for part in BASELINE["split"].values()) <= 101
    valid = sum(e["valid"] for e in BASELINE["okrugs"].values())
    er = sum(e["votes"]["Единая Россия"] for e in BASELINE["okrugs"].values()) * 100 / valid
    er_official = sum(e["votes"]["Единая Россия"] for e in old.values()) * 100 / sum(e["valid"] for e in old.values())
    assert abs(er - er_official) < 3  # the recalculated okrugs add up to the official regional result (share of all ballots vs of valid)


def test_roster_for_a_chosen_past_day(settings):
    from app.main import build_roster
    now = datetime(2026, 9, 20, 15, 0, tzinfo=settings.zone)
    values = roster_values(settings, [("2026-09-17", BAL, "Старый", "Давно", 4), ("2026-09-18", BAL, "Иванова", "Анна", 5),
                                      ("2026-09-19", BAL, "Иванова", "Анна", 2), ("2026-09-19", DMI, "Петров", "Олег", 3),
                                      ("2026-09-20", BAL, "Иванова", "Анна", 1)])
    live = build_roster(values, settings, now)
    assert live["live"] is True and live["today"] == "2026-09-20" and live["available_days"] == ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"]
    past = build_roster(values, settings, now, day="2026-09-19")
    assert past["live"] is False and past["today"] == "2026-09-19" and past["days"] == ["2026-09-17", "2026-09-18", "2026-09-19"]  # nothing after the chosen day
    people = {p["name"]: p for o in past["okrugs"] for p in o["people"]}
    assert people["Иванова Анна"]["status"] == "both" and people["Иванова Анна"]["today"] == 2 and "2026-09-20" not in people["Иванова Анна"]["by_day"]
    assert people["Петров Олег"]["status"] == "new"  # first seen on the 19th
    assert people["Старый Давно"]["status"] == "absent" and people["Старый Давно"]["last_day"] == "2026-09-17"
    assert all(p["activity"] is None and p["minutes_since"] is None for p in people.values())  # live activity only for the running day
    assert build_roster(values, settings, now, day="2026-09-30")["today"] == "2026-09-20"  # a future day falls back to today


def test_roster_endpoint_takes_a_day(settings, monkeypatch):
    settings.dashboard_code = "coordinator-secret"
    settings.spreadsheet = "test-sheet"
    monkeypatch.setattr("app.main.read_sheet", lambda _: [])
    monkeypatch.setattr("app.main.read_anomaly_statuses", lambda _s, session=None: {})
    with TestClient(create_app(settings)) as client:
        client.post("/api/dashboard/login", json={"code": "coordinator-secret"})
        client.post("/api/dashboard/roster/login", json={"code": "exitpoll"})
        assert client.get("/api/dashboard/roster", params={"day": "2026-09-18"}).status_code == 200
        assert client.get("/api/dashboard/roster", params={"day": "oops"}).status_code == 422


def test_import_backup_script_picks_only_undelivered_surveys():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("import_backup", Path(__file__).resolve().parent.parent / "scripts" / "import_backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backup = {"exported_at": "x", "surveys": [{"id": "1", "party": "1", "received": True}, {"id": "2", "party": "1", "received": False},
                                              {"id": "3", "party": "1", "received": False, "rejected": True, "problem": "x"},
                                              {"id": "4", "party": "1", "received": False, "problem": None}]}
    pending = module.pending_surveys(backup)
    assert [s["id"] for s in pending] == ["2", "4"] and all(set(s) == {"id", "party"} for s in pending)
    assert [s["id"] for s in module.pending_surveys(backup, send_all=True)] == ["1", "2", "3", "4"]
    assert [len(p) for p in module.packs(list(range(120)))] == [50, 50, 20]


def test_interviewer_app_sends_in_packs_with_timeouts_and_a_new_cache_version():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "app" / "static"
    script = (static / "app.js").read_text(encoding="utf-8")
    assert "/api/surveys/batch" in script and "const SYNC_PACK = 50" in script and "20000" in script  # packs of 50, 20 s per pack
    assert "storageTimeout(" in script and "Память телефона не отвечает" in script  # a hung IndexedDB is reported, not silent
    assert "sendOneByOne" in script  # an older server without the batch endpoint still works
    assert "connection(false); return;" in script  # "no connection" only when the health check fails
    assert "exit-poll-v8-offline" in (static / "sw.js").read_text(encoding="utf-8")

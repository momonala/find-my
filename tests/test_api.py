"""Tests for src/api.py.

Uses `create_app(start_poller=False)` against a temp DB (monkeypatched via
`src.db.DB_PATH`) so routes are exercised without any live Apple fetch, and
data is seeded directly through `src.db.record_fetch`. See tests/conftest.py
for the `client`/`seed` fixtures.
"""

import src.api as api
from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later


def test_list_locations_is_empty_with_no_data(client):
    assert client.get("/locations").get_json() == []


def test_list_locations_returns_seeded_devices(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)), make_item("no-fix"))

    body = client.get("/locations").get_json()
    assert {row["id"] for row in body} == {"tag-1", "no-fix"}


def test_get_single_location(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    body = client.get("/locations/tag-1").get_json()
    assert body["latitude"] == 52.5
    assert body["longitude"] == 13.4


def test_get_unknown_location_is_404(client):
    assert client.get("/locations/does-not-exist").status_code == 404


def test_history_accumulates_on_movement_only(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    seed(make_item("tag-1", make_location(52.5, 13.4, minutes_later(1))))  # unchanged
    seed(make_item("tag-1", make_location(52.6, 13.4, minutes_later(2))))  # moved

    assert len(client.get("/locations/tag-1/history").get_json()) == 2


def test_history_limit_param(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    seed(make_item("tag-1", make_location(52.6, 13.4, minutes_later(1))))

    assert len(client.get("/locations/tag-1/history?limit=1").get_json()) == 1


def test_history_for_unknown_device_is_404(client):
    assert client.get("/locations/does-not-exist/history").status_code == 404


def test_index_redirects_to_dashboard(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard"


def test_dashboard_page_loads(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"Find My dashboard" in response.data


def test_dashboard_static_assets_are_served(client):
    assert client.get("/static/dashboard.css").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200


def test_config_exposes_home_coordinates_and_telegram_status(client, monkeypatch):
    monkeypatch.setattr(api, "TELEGRAM_API_TOKEN", "")
    monkeypatch.setattr(api, "TELEGRAM_CHAT_ID", "")
    body = client.get("/config").get_json()
    assert body == {
        "home_latitude": HOME_LATITUDE,
        "home_longitude": HOME_LONGITUDE,
        "telegram_configured": False,
    }


def test_config_reports_telegram_configured_when_both_set(client, monkeypatch):
    monkeypatch.setattr(api, "TELEGRAM_API_TOKEN", "token")
    monkeypatch.setattr(api, "TELEGRAM_CHAT_ID", "chat")
    body = client.get("/config").get_json()
    assert body["telegram_configured"] is True


def test_status_is_null_before_any_fetch(client):
    assert client.get("/status").get_json() == {"last_updated": None}


def test_status_reflects_last_record_fetch(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.get("/status").get_json()["last_updated"] is not None


def test_locations_include_source(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.get("/locations").get_json()[0]["source"] == "item"


def test_distance_is_computed_server_side(client, seed):
    """The dashboard reads `distance_m` rather than recomputing a haversine."""
    seed(make_item("at-home", make_location(HOME_LATITUDE, HOME_LONGITUDE)))
    seed(make_item("no-fix"))

    rows = {row["id"]: row for row in client.get("/locations").get_json()}
    assert rows["at-home"]["distance_m"] == 0
    assert rows["no-fix"]["distance_m"] is None


def test_default_icon_used_for_known_apple_kinds(client, seed):
    seed(make_item("phone", make_location(52.5, 13.4), kind="iPhone", source="device"))

    assert client.get("/locations/phone").get_json()["icon"] == "📱"


# --- PUT /locations/<id>/icon --------------------------------------------------


def test_put_icon_sets_it_and_returns_the_full_device(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    body = client.put("/locations/tag-1/icon", json={"emoji": "🚲"}).get_json()

    # Same shape as GET, so a client can re-render straight from the response.
    assert body["id"] == "tag-1"
    assert body["icon"] == "🚲"
    assert body["latitude"] == 52.5
    assert client.get("/locations/tag-1").get_json()["icon"] == "🚲"


def test_put_icon_with_null_emoji_clears_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    client.put("/locations/tag-1/icon", json={"emoji": "🚲"})

    client.put("/locations/tag-1/icon", json={"emoji": None})

    assert client.get("/locations/tag-1").get_json()["icon"] is None


def test_put_icon_with_blank_string_clears_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    client.put("/locations/tag-1/icon", json={"emoji": "🚲"})

    client.put("/locations/tag-1/icon", json={"emoji": "   "})

    assert client.get("/locations/tag-1").get_json()["icon"] is None


def test_put_icon_for_unknown_device_is_404(client):
    assert client.put("/locations/does-not-exist/icon", json={"emoji": "🚲"}).status_code == 404


def test_put_icon_rejects_missing_body(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    # An empty body used to silently clear the icon instead of 400-ing.
    assert client.put("/locations/tag-1/icon").status_code == 400


def test_put_icon_rejects_non_string_emoji(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.put("/locations/tag-1/icon", json={"emoji": 42}).status_code == 400


def test_put_icon_rejects_overlong_emoji(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.put("/locations/tag-1/icon", json={"emoji": "x" * 64})

    assert response.status_code == 400
    assert client.get("/locations/tag-1").get_json()["icon"] is None


def test_put_icon_rejects_control_characters(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.put("/locations/tag-1/icon", json={"emoji": "a\x00b"}).status_code == 400


def test_put_icon_requires_token_when_configured(client, seed, monkeypatch):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    monkeypatch.setattr(api, "API_WRITE_TOKEN", "s3cret")

    assert client.put("/locations/tag-1/icon", json={"emoji": "🚲"}).status_code == 401

    response = client.put(
        "/locations/tag-1/icon",
        json={"emoji": "🚲"},
        headers={"X-Api-Token": "s3cret"},
    )
    assert response.status_code == 200


def test_reads_are_open_even_when_a_write_token_is_configured(client, seed, monkeypatch):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    monkeypatch.setattr(api, "API_WRITE_TOKEN", "s3cret")

    assert client.get("/locations").status_code == 200


# --- /alerts --------------------------------------------------------------


def test_list_alerts_is_empty_with_no_data(client):
    assert client.get("/alerts").get_json() == []


def test_post_alert_creates_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post(
        "/alerts", json={"device_id": "tag-1", "alert_type": "movement", "threshold_m": 150}
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["device_id"] == "tag-1"
    assert body["device_name"] == "Item tag-1"
    assert body["alert_type"] == "movement"
    assert body["threshold_m"] == 150
    assert body["is_active"] is False
    assert body["triggered_at"] is None
    assert body["anchor_lat"] is None
    assert body["anchor_lon"] is None
    assert len(client.get("/alerts").get_json()) == 1


def test_post_enter_alert_defaults_to_a_home_anchor(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post("/alerts", json={"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100})

    assert response.get_json()["anchor_lat"] is None
    assert response.get_json()["anchor_lon"] is None


def test_post_enter_alert_with_current_anchor_snapshots_the_devices_location(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post(
        "/alerts",
        json={"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    body = response.get_json()
    assert response.status_code == 201
    assert body["anchor_lat"] == 52.5
    assert body["anchor_lon"] == 13.4


def test_post_alert_with_current_anchor_and_no_fix_yet_is_400(client, seed):
    seed(make_item("tag-1"))  # device known, no fix yet

    response = client.post(
        "/alerts",
        json={"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    assert response.status_code == 400


def test_post_alert_with_current_anchor_for_unknown_device_is_404(client):
    response = client.post(
        "/alerts",
        json={"device_id": "does-not-exist", "alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    assert response.status_code == 404


def test_post_alert_rejects_invalid_anchor(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post(
        "/alerts", json={"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100, "anchor": "bogus"}
    )

    assert response.status_code == 400


def test_post_alert_for_unknown_device_is_404(client):
    response = client.post(
        "/alerts", json={"device_id": "does-not-exist", "alert_type": "movement", "threshold_m": 100}
    )
    assert response.status_code == 404


def test_post_alert_rejects_invalid_alert_type(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post("/alerts", json={"device_id": "tag-1", "alert_type": "bogus", "threshold_m": 100})

    assert response.status_code == 400


def test_post_alert_rejects_non_positive_threshold(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post("/alerts", json={"device_id": "tag-1", "alert_type": "movement", "threshold_m": 0})

    assert response.status_code == 400


def test_post_alert_rejects_non_numeric_threshold(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post(
        "/alerts", json={"device_id": "tag-1", "alert_type": "movement", "threshold_m": "far"}
    )

    assert response.status_code == 400


def test_post_alert_rejects_missing_body(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.post("/alerts").status_code == 400


def test_delete_alert_removes_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    created = client.post(
        "/alerts", json={"device_id": "tag-1", "alert_type": "movement", "threshold_m": 100}
    )
    alert_id = created.get_json()["id"]

    response = client.delete(f"/alerts/{alert_id}")

    assert response.status_code == 204
    assert client.get("/alerts").get_json() == []


def test_delete_unknown_alert_is_404(client):
    assert client.delete("/alerts/999").status_code == 404


def test_post_alert_requires_token_when_configured(client, seed, monkeypatch):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    monkeypatch.setattr(api, "API_WRITE_TOKEN", "s3cret")
    payload = {"device_id": "tag-1", "alert_type": "movement", "threshold_m": 100}

    assert client.post("/alerts", json=payload).status_code == 401

    response = client.post("/alerts", json=payload, headers={"X-Api-Token": "s3cret"})
    assert response.status_code == 201


def test_delete_alert_requires_token_when_configured(client, seed, monkeypatch):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    created = client.post(
        "/alerts", json={"device_id": "tag-1", "alert_type": "movement", "threshold_m": 100}
    )
    alert_id = created.get_json()["id"]
    monkeypatch.setattr(api, "API_WRITE_TOKEN", "s3cret")

    assert client.delete(f"/alerts/{alert_id}").status_code == 401

    response = client.delete(f"/alerts/{alert_id}", headers={"X-Api-Token": "s3cret"})
    assert response.status_code == 204


def test_reads_of_alerts_are_open_even_when_a_write_token_is_configured(client, seed, monkeypatch):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    monkeypatch.setattr(api, "API_WRITE_TOKEN", "s3cret")

    assert client.get("/alerts").status_code == 200

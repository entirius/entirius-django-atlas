# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stage 6: process_scraper_results_task.

Tests cover happy path, mixed validation errors, all invalid, unknown run_id,
empty payload, idempotent re-delivery, acks_late introspection, validation typing.
"""

import uuid

import pytest
from django.utils import timezone

from django_atlas.enums import LogStatus
from django_atlas.models import ImportLog, IntegrationEvent, Source, SourceFeed
from django_atlas.tasks.scraper_callback import process_scraper_results_task


@pytest.fixture
def feed(db, language, currency):
    s = Source.objects.create(idx="sup-cb", name="CB", default_language=language, default_currency=currency)
    return SourceFeed.objects.create(source=s, idx="feed-cb", connector_kind="scraper", feed_config={"foo": "bar"})


@pytest.fixture
def running_log(db, feed):
    return ImportLog.objects.create(
        feed=feed,
        run_id=uuid.uuid4(),
        mode="full",
        status=LogStatus.RUNNING.value,
        source="scheduler",
        started_at=timezone.now(),
    )


@pytest.mark.django_db
def test_happy_path_processes_valid_payload(running_log, monkeypatch):
    captured = {}

    def fake_process(run_uuid, validated):
        captured["run_uuid"] = run_uuid
        captured["count"] = len(validated)
        running_log.status = LogStatus.SUCCESS.value
        running_log.new_count = len(validated)
        running_log.save()
        return running_log

    monkeypatch.setattr("django_atlas.tasks.scraper_callback.import_service.process_feed_results", fake_process)

    payload = [
        {"external_id": "EXT-1", "name": "X", "cost": "10.00", "currency": "EUR"},
        {"external_id": "EXT-2", "name": "Y", "cost": "20.00", "currency": "EUR"},
    ]
    result = process_scraper_results_task(str(running_log.run_id), payload)
    assert result["status"] == LogStatus.SUCCESS.value
    assert captured["count"] == 2


@pytest.mark.django_db
def test_mixed_valid_and_invalid_emits_event_and_processes_valid(running_log, monkeypatch):
    monkeypatch.setattr(
        "django_atlas.tasks.scraper_callback.import_service.process_feed_results",
        lambda run_uuid, validated: running_log,
    )
    payload = [
        {"external_id": "EXT-1", "name": "X", "cost": "10.00", "currency": "EUR"},
        {"missing_required_fields": True},
    ]
    process_scraper_results_task(str(running_log.run_id), payload)
    events = IntegrationEvent.objects.filter(event_type="scraper_results_validation_errors")
    assert events.exists()
    assert events.first().details["count"] == 1


@pytest.mark.django_db
def test_all_invalid_finalizes_partial(running_log):
    payload = [{"missing": True}, {"also_missing": True}]
    result = process_scraper_results_task(str(running_log.run_id), payload)
    assert result["status"] == LogStatus.PARTIAL.value
    assert result["error_count"] == 2


@pytest.mark.django_db
def test_unknown_run_id_emits_event(db):
    fake_id = str(uuid.uuid4())
    result = process_scraper_results_task(fake_id, [])
    assert result["status"] == "error"
    assert IntegrationEvent.objects.filter(event_type="scraper_unknown_run_id").exists()


@pytest.mark.django_db
def test_empty_payload_handled_gracefully(running_log, monkeypatch):
    captured = {"called": False}

    def fake_process(*a, **kw):
        captured["called"] = True
        running_log.status = LogStatus.SUCCESS.value
        running_log.save()
        return running_log

    monkeypatch.setattr("django_atlas.tasks.scraper_callback.import_service.process_feed_results", fake_process)
    result = process_scraper_results_task(str(running_log.run_id), [])
    assert result["status"] == LogStatus.SUCCESS.value
    # process_feed_results IS called with empty list (idempotent).
    assert captured["called"] is True


@pytest.mark.django_db
def test_duplicate_run_id_already_finalized(running_log):
    running_log.status = LogStatus.SUCCESS.value
    running_log.new_count = 5
    running_log.updated_count = 2
    running_log.error_count = 1
    running_log.save()
    result = process_scraper_results_task(str(running_log.run_id), [])
    assert result["status"] == LogStatus.SUCCESS.value
    assert result["success_count"] == 7
    assert result["error_count"] == 1
    # Sanity: warning event recorded
    assert IntegrationEvent.objects.filter(event_type="scraper_unknown_run_id").exists()


def test_acks_late_is_true():
    # Introspection on the task definition.
    assert process_scraper_results_task.acks_late is True


@pytest.mark.django_db
def test_invalid_run_id_string(db):
    result = process_scraper_results_task("not-a-uuid", [])
    assert result["status"] == "error"


@pytest.mark.django_db
def test_validation_errors_in_event_details_are_sanitized(running_log):
    """C6: raw Pydantic exception text MUST NOT land in event.details (validator names leak)."""
    bad_payload = [{"external_id": "BAD", "name": "X", "cost": "not-a-decimal", "currency": "EUR"}]
    process_scraper_results_task(str(running_log.run_id), bad_payload)
    event = IntegrationEvent.objects.filter(event_type="scraper_results_validation_errors").first()
    assert event is not None
    sample_text = str(event.details)
    # Sanitized: no Pydantic-internal validator names / class names / hint URLs
    assert "ValidationError" not in sample_text
    assert "pydantic" not in sample_text.lower()
    # Structured (loc, type) format expected
    sample = event.details.get("sample") or []
    if sample:
        first = sample[0]
        assert "fields" in first
        assert isinstance(first["fields"], list)


# HMAC signature (Faza 9) ------------------------------------------------


@pytest.mark.django_db
def test_hmac_disabled_by_default_legacy_callers_still_work(running_log, monkeypatch):
    """Default empty secret = signature not enforced (single-tenant deploy)."""
    monkeypatch.setattr("django_atlas.tasks.scraper_callback.source_settings.ATLAS_SCRAPER_CALLBACK_HMAC_SECRET", "")
    result = process_scraper_results_task(str(running_log.run_id), [])
    # No signature_invalid event emitted
    assert not IntegrationEvent.objects.filter(event_type="scraper_callback_signature_invalid").exists()
    assert result["status"] != "error" or "signature" not in str(result)


@pytest.mark.django_db
def test_hmac_enabled_missing_signature_rejected(running_log, monkeypatch):
    monkeypatch.setattr(
        "django_atlas.tasks.scraper_callback.source_settings.ATLAS_SCRAPER_CALLBACK_HMAC_SECRET", "topsecret"
    )
    result = process_scraper_results_task(str(running_log.run_id), [{"external_id": "x", "name": "y"}], signature=None)
    assert result["status"] == "error"
    assert IntegrationEvent.objects.filter(event_type="scraper_callback_signature_invalid").exists()


@pytest.mark.django_db
def test_hmac_enabled_wrong_signature_rejected(running_log, monkeypatch):
    monkeypatch.setattr(
        "django_atlas.tasks.scraper_callback.source_settings.ATLAS_SCRAPER_CALLBACK_HMAC_SECRET", "topsecret"
    )
    result = process_scraper_results_task(
        str(running_log.run_id), [{"external_id": "x", "name": "y"}], signature="deadbeef"
    )
    assert result["status"] == "error"
    assert IntegrationEvent.objects.filter(event_type="scraper_callback_signature_invalid").exists()


@pytest.mark.django_db
def test_hmac_enabled_valid_signature_accepted(running_log, monkeypatch):
    from django_atlas.tasks.scraper_callback import compute_signature

    secret = "topsecret"
    monkeypatch.setattr(
        "django_atlas.tasks.scraper_callback.source_settings.ATLAS_SCRAPER_CALLBACK_HMAC_SECRET", secret
    )
    payload = [{"external_id": "x", "name": "y", "cost": "1.00", "currency": "EUR"}]
    sig = compute_signature(str(running_log.run_id), payload, secret)
    monkeypatch.setattr(
        "django_atlas.tasks.scraper_callback.import_service.process_feed_results",
        lambda run_uuid, validated: running_log,
    )
    result = process_scraper_results_task(str(running_log.run_id), payload, signature=sig)
    # Did NOT short-circuit on signature_invalid
    assert not IntegrationEvent.objects.filter(event_type="scraper_callback_signature_invalid").exists()
    assert result["status"] != "error"

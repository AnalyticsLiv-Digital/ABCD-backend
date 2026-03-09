import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from schemas import JobStatus


client = TestClient(app)


def _auth_headers_for(email: str = "user@example.com") -> dict:
  # We stub get_current_user so token value is irrelevant.
  return {"Authorization": "Bearer dummy-token"}


def test_create_job_with_extended_metadata_uses_mock_result_when_abcd_disabled(monkeypatch):
  # Stub auth to always return a user
  with patch("routers.auth.get_current_user") as mock_user_dep, patch(
      "routers.jobs.get_current_user"
  ) as mock_jobs_dep:
    user = {"email": "user@example.com"}
    mock_user_dep.return_value = user
    mock_jobs_dep.return_value = user

    # Allow quota
    monkeypatch.setattr(
        "routers.jobs.can_consume_run_and_increment",
        lambda current_user: True,
        raising=False,
    )

    # Force mock mode
    monkeypatch.setattr("config.settings.USE_REAL_ABCD", False, raising=False)

    body = {
        "youtube_url": "https://www.youtube.com/watch?v=dummy",
        "brand_name": "Acme",
        "brand_variations": ["Acme Co", "Acme Corp"],
        "products": ["SuperWidget"],
        "product_categories": ["Gadgets"],
        "call_to_actions": ["Sign up", "Buy now"],
        "campaign_name": "Q4 Launch",
        "campaign_tags": ["holiday-2025"],
        "creative_format": "long_form",
        "objective": "awareness",
        "advanced": {
            "enable_llms": True,
            "enable_annotations": False,
            "allow_public_share": False,
        },
    }

    resp = client.post("/jobs", json=body, headers=_auth_headers_for())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "job_id" in data
    job_id = data["job_id"]

    # Immediately fetch the job; in mock mode it should eventually complete with a mock result.
    # We poll a few times with small sleeps to give BackgroundTasks time to run.
    import time

    for _ in range(10):
      job_resp = client.get(f"/jobs/{job_id}", headers=_auth_headers_for())
      assert job_resp.status_code == 200
      job_data = job_resp.json()
      if job_data["status"] == JobStatus.COMPLETED:
        break
      time.sleep(0.2)

    assert job_data["status"] == JobStatus.COMPLETED
    assert job_data["result"] is not None
    assert job_data["result"]["result_source"] == "mock"
    assert job_data["result"]["brand_name"] == "Acme"


def test_run_real_abcd_propagates_brand_and_advanced_metadata(monkeypatch):
  """Verify that when real ABCD is available, metadata is passed into run_abcd_analysis."""
  # Stub auth
  with patch("routers.auth.get_current_user") as mock_user_dep, patch(
      "routers.jobs.get_current_user"
  ) as mock_jobs_dep:
    user = {"email": "user2@example.com"}
    mock_user_dep.return_value = user
    mock_jobs_dep.return_value = user

    # Allow quota
    monkeypatch.setattr(
        "routers.jobs.can_consume_run_and_increment",
        lambda current_user: True,
        raising=False,
    )

    # Pretend real ABCD is available
    monkeypatch.setattr("abcd_service.is_real_abcd_available", lambda: True, raising=False)

    captured_kwargs = {}

    def fake_run_abcd_analysis(**kwargs):
      nonlocal captured_kwargs
      captured_kwargs = kwargs
      # Return a minimal JobResultPayload-like dict to satisfy set_job_completed
      return MagicMock(
          video_uri=kwargs["video_uri"],
          brand_name=kwargs["brand_name"],
          result_source="abcd",
          overall_score_pct=100.0,
          overall_result="Excellent",
          long_form_abcd=[],
          shorts=[],
      )

    monkeypatch.setattr("routers.jobs.run_abcd_analysis", fake_run_abcd_analysis, raising=False)

    body = {
        "youtube_url": "https://www.youtube.com/watch?v=real",
        "brand_name": "BrandX",
        "brand_variations": ["Brand X"],
        "products": ["Prod1"],
        "product_categories": ["Cat1"],
        "call_to_actions": ["Learn more"],
        "campaign_name": "Campaign",
        "campaign_tags": ["tag1"],
        "creative_format": "shorts",
        "objective": "consideration",
        "advanced": {
            "enable_llms": True,
            "enable_annotations": False,
            "allow_public_share": True,
        },
    }

    resp = client.post("/jobs", json=body, headers=_auth_headers_for())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "job_id" in data

    # Give background task a moment to run fake_run_abcd_analysis
    import time

    for _ in range(10):
      if captured_kwargs:
        break
      time.sleep(0.1)

    # Ensure metadata made it through to the analysis call
    assert captured_kwargs, "run_abcd_analysis was not called"
    assert captured_kwargs["brand_name"] == "BrandX"
    assert captured_kwargs["brand_variations"] == ["Brand X"]
    assert captured_kwargs["products"] == ["Prod1"]
    assert captured_kwargs["product_categories"] == ["Cat1"]
    assert captured_kwargs["call_to_actions"] == ["Learn more"]
    assert captured_kwargs["creative_format"] == "shorts"
    assert captured_kwargs["advanced"]["enable_llms"] is True
    assert captured_kwargs["advanced"]["enable_annotations"] is False
    assert captured_kwargs["advanced"]["allow_public_share"] is True


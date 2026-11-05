import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from app.models.survey import SurveySubmission


_SCHEMA = """
CREATE TABLE IF NOT EXISTS survey_responses (
  response_id TEXT PRIMARY KEY,
  submitted_at TEXT NOT NULL,
  frequency TEXT NOT NULL,
  features_json TEXT NOT NULL,
  source TEXT NOT NULL,
  pricing_model TEXT NOT NULL,
  price_range TEXT NOT NULL,
  feature_request TEXT NOT NULL,
  task_outcome TEXT NOT NULL,
  issue_description TEXT NOT NULL,
  usage_scenario TEXT NOT NULL,
  contact TEXT NOT NULL,
  feature_other TEXT NOT NULL,
  source_other TEXT NOT NULL,
  scenario_other TEXT NOT NULL,
  page_path TEXT NOT NULL,
  client_id TEXT NOT NULL UNIQUE,
  schema_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_survey_submitted_at ON survey_responses(submitted_at);
"""

_CST = timezone(timedelta(hours=8))


class SurveyStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def save(self, payload: SurveySubmission) -> bool:
        response_id = str(uuid.uuid4())
        submitted_at = datetime.now(_CST).isoformat(timespec="seconds")
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO survey_responses (
                      response_id, submitted_at, frequency, features_json, source,
                      pricing_model, price_range, feature_request, task_outcome,
                      issue_description, usage_scenario, contact, feature_other,
                      source_other, scenario_other, page_path, client_id, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        response_id,
                        submitted_at,
                        payload.frequency,
                        json.dumps(payload.features, ensure_ascii=False),
                        payload.source,
                        payload.pricing_model,
                        payload.price_range,
                        payload.feature_request,
                        payload.task_outcome,
                        payload.issue_description,
                        payload.usage_scenario,
                        payload.contact,
                        payload.feature_other,
                        payload.source_other,
                        payload.scenario_other,
                        payload.page_path,
                        payload.client_id,
                        payload.schema_version,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

"""Pipeline alerting: every alert is logged and written to data/alerts; if a webhook URL is
configured (Slack/Teams-compatible JSON ``{"text": ...}``) it is posted there as well."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests

from loanlens.config import Settings

log = logging.getLogger(__name__)


def send_alert(settings: Settings, title: str, details: str, severity: str = "error") -> None:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    payload = {"title": title, "severity": severity, "details": details,
               "raised_at": datetime.now().isoformat(), "profile": settings.profile}
    settings.alerts_dir.mkdir(parents=True, exist_ok=True)
    (settings.alerts_dir / f"alert_{stamp}.json").write_text(json.dumps(payload, indent=2))
    log.error("ALERT %s: %s\n%s", severity.upper(), title, details)

    url = os.environ.get(settings["ingest"]["alert_webhook_env"])
    if url:
        try:
            requests.post(url, json={"text": f":rotating_light: *{title}*\n```{details[:3500]}```"},
                          timeout=10)
        except requests.RequestException as exc:  # alerting must never mask the real error
            log.warning("Alert webhook failed: %s", exc)

import logging
import sys
import time
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("trigger_incident")

TARGET_CRASH_URL = "http://localhost:8080/simulate_crash"
WEBHOOK_ALERT_URL = "http://localhost:5000/alert"


def crash_target_service():
    """Triggers fatal crash and error logs on dummy_prod_api."""
    logger.info("Sending crash simulation request to target service at %s...", TARGET_CRASH_URL)
    try:
        response = requests.post(TARGET_CRASH_URL, timeout=5)
        if response.status_code == 500:
            logger.info("Target service responded with expected crash status 500: %s", response.text.strip())
        else:
            logger.info("Target service responded with status code: %d", response.status_code)
    except requests.exceptions.RequestException as exc:
        logger.info("Target service crashed / error during simulation: %s", exc)


def send_incident_alert():
    """Fires incident alert payload to the SRE webhook listener."""
    logger.info("Dispatching incident alert to webhook listener at %s...", WEBHOOK_ALERT_URL)
    payload = {
        "service": "dummy_prod_api",
        "severity": "CRITICAL",
        "issue": "Healthcheck failed - 500 Internal Server Error: Database deadlock",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    try:
        response = requests.post(WEBHOOK_ALERT_URL, json=payload, timeout=5)
        logger.info("Webhook listener response [%d]: %s", response.status_code, response.text)
        if response.status_code == 202:
            logger.info("Alert successfully accepted for background triage!")
        else:
            logger.warning("Unexpected response status code: %d", response.status_code)
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to deliver alert to webhook listener: %s", exc)
        logger.error("Ensure api/webhook_listener.py is running on port 5000.")


def main():
    """Executes full incident simulation cycle."""
    logger.info("=== STEP 1: Simulating Microservice Crash ===")
    crash_target_service()

    time.sleep(1)

    logger.info("=== STEP 2: Triggering SRE Webhook Alert ===")
    send_incident_alert()
    logger.info("Incident trigger cycle finished.")


if __name__ == "__main__":
    main()

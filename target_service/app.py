import logging
import time
from fastapi import FastAPI, HTTPException, Response, status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("dummy_prod_api")

app = FastAPI(title="Dummy Production Microservice", version="1.0.0")

# Simulated state for the microservice
state = {
    "is_crashed": False,
    "crash_reason": None,
    "crash_time": None
}


@app.get("/health")
def health_check(response: Response):
    """Health check endpoint monitored by SRE alert systems."""
    try:
        if state["is_crashed"]:
            logger.error(
                "Health check FAILED: %s (crashed at %s)",
                state["crash_reason"],
                state["crash_time"]
            )
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return {
                "status": "unhealthy",
                "error": state["crash_reason"],
                "timestamp": time.time()
            }
        
        logger.info("Health check passed: service normal")
        return {"status": "ok", "service": "dummy_prod_api", "timestamp": time.time()}
    except Exception as exc:
        logger.exception("Unexpected error in health check: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/simulate_crash")
@app.get("/simulate_crash")
def simulate_crash():
    """Artificially triggers a fatal service crash and emits critical error logs."""
    try:
        state["is_crashed"] = True
        state["crash_reason"] = "CRITICAL_DATABASE_POOL_EXHAUSTION: Thread deadlock detected"
        state["crash_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        logger.critical("==================================================")
        logger.critical("FATAL: Unhandled exception in background worker pool")
        logger.critical("Error: ConnectionPoolTimeout - pool full (100/100 connections)")
        logger.critical("Worker pid: 42 thread 0x7f9a killed by watchdog")
        logger.critical("Service state transitioning to UNHEALTHY")
        logger.critical("==================================================")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Simulated fatal crash initiated. Service is now unhealthy."
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to execute crash simulation: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/recover")
def recover():
    """Manual recovery endpoint to reset state without restarting container."""
    state["is_crashed"] = False
    state["crash_reason"] = None
    state["crash_time"] = None
    logger.info("Service manually recovered to healthy state")
    return {"status": "ok", "message": "Service recovered"}

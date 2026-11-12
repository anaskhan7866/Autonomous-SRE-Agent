import logging
import os
from typing import Any, Dict, List

import chromadb
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("setup_runbooks")

CHROMA_PATH = "./data/chroma"
COLLECTION_NAME = "sre_runbooks"
RUNBOOK_ID_PREFIX = "runbook_"

# Each runbook has a stable "id" (never derived from list position), so reordering is safe.
RUNBOOK_DOCUMENTS: List[Dict[str, Any]] = [
    {
        "id": "dummy_prod_api_disk_cache_full",
        "content": (
            "Runbook: dummy_prod_api disk full or cache full. Symptoms: 'No space left on device', "
            "'/tmp/cache full', stale cache files, write failures, disk usage at 100%. "
            "Authorized mitigation: clear the cache with EXEC dummy_prod_api rm -rf /tmp/cache. "
            "Do NOT restart first: a restart does not free disk space."
        ),
        "metadata": {
            "source": "runbook_dummy_prod_api_disk.md",
            "service": "dummy_prod_api",
            "incident_type": "disk_cache_full",
            "action": "exec_clear_cache"
        }
    },
    {
        "id": "dummy_prod_api_diagnose_logs",
        "content": (
            "Runbook: dummy_prod_api log inspection for unclear errors. Symptoms: unexplained 500 errors, "
            "unknown exceptions, intermittent errors with no clear cause in the logs. "
            "Diagnose first with EXEC dummy_prod_api tail -n 50 /var/log/app.log or "
            "EXEC dummy_prod_api df -h. Do not restart until the diagnosis points to a crash or deadlock."
        ),
        "metadata": {
            "source": "runbook_dummy_prod_api_diagnose.md",
            "service": "dummy_prod_api",
            "incident_type": "unclear_errors",
            "action": "exec_diagnose"
        }
    },
    {
        "id": "dummy_prod_api_restart",
        "content": (
            "Runbook: dummy_prod_api memory spike, deadlock, connection pool exhausted or health check failure. "
            "Symptoms: out of memory, process hung, connection pool deadlock, /health not returning 200. "
            "Authorized mitigation: RESTART dummy_prod_api."
        ),
        "metadata": {
            "source": "runbook_dummy_prod_api.md",
            "service": "dummy_prod_api",
            "incident_type": "deadlock_memory_health_failure",
            "action": "restart_container"
        }
    },
    {
        "id": "redis_cache_restart",
        "content": (
            "Incident Response Protocol: If the redis_cache service experiences "
            "a ConnectionError, timeout, or OOM (Out of Memory) crash, the authorized "
            "mitigation action is to restart the Docker container named redis_cache."
        ),
        "metadata": {
            "source": "runbook_redis_cache.md",
            "service": "redis_cache",
            "incident_type": "timeout_connection_error",
            "action": "restart_container"
        }
    }
]


def initialize_vector_db() -> Chroma:
    """Initializes local persistent ChromaDB with HuggingFace embeddings."""
    try:
        os.makedirs(CHROMA_PATH, exist_ok=True)
        embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=CHROMA_PATH)

        vector_db = Chroma(
            client=client,
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings
        )
        return vector_db
    except Exception as exc:
        logger.exception("Failed to initialize vector database: %s", exc)
        raise


def inject_runbooks(vector_db: Chroma) -> None:
    """Idempotently replaces runbooks using stable ids; removes stale runbook_* ids no longer defined."""
    try:
        texts = [doc["content"] for doc in RUNBOOK_DOCUMENTS]
        metadatas = [doc["metadata"] for doc in RUNBOOK_DOCUMENTS]
        ids = [f"{RUNBOOK_ID_PREFIX}{doc['id']}" for doc in RUNBOOK_DOCUMENTS]

        existing_ids = (vector_db.get() or {}).get("ids") or []
        stale = [i for i in existing_ids if i.startswith(RUNBOOK_ID_PREFIX) and i not in ids]
        to_delete = stale + [i for i in ids if i in existing_ids]
        if to_delete:
            logger.info("Removing %d existing/stale runbook document(s)...", len(to_delete))
            vector_db.delete(ids=to_delete)

        vector_db.add_texts(texts=texts, metadatas=metadatas, ids=ids)
        logger.info("Successfully indexed %d runbook document(s)", len(texts))
    except Exception as exc:
        logger.exception("Failed to inject runbooks: %s", exc)
        raise


def verify_runbook_retrieval(vector_db: Chroma) -> None:
    """Verifies that retrieval functions accurately via similarity search."""
    try:
        query = "dummy_prod_api service health check failed deadlock"
        results = vector_db.similarity_search(query, k=1)
        if not results:
            raise RuntimeError("Verification failed: no matching documents retrieved.")

        logger.info("Verification passed! Retrieved runbook preview:")
        logger.info("Content: %s", results[0].page_content)
        logger.info("Metadata: %s", results[0].metadata)
    except Exception as exc:
        logger.exception("Verification query failed: %s", exc)
        raise


def main() -> None:
    """Entry point to setup and verify persistent runbooks vector database."""
    logger.info("Starting runbook vector DB setup at '%s'...", CHROMA_PATH)
    vdb = initialize_vector_db()
    inject_runbooks(vdb)
    verify_runbook_retrieval(vdb)
    logger.info("Runbook setup complete!")


if __name__ == "__main__":
    main()

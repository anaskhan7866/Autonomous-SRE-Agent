import logging
import os
import chromadb
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("setup_runbooks")

CHROMA_PATH = "./data/chroma"
COLLECTION_NAME = "sre_runbooks"

RUNBOOK_DOCUMENTS = [
    {
        "content": (
            "Incident Response Protocol: If the dummy_prod_api service experiences "
            "a deadlock, memory spike, or health check failure, the authorized "
            "mitigation action is to restart the Docker container named dummy_prod_api."
        ),
        "metadata": {
            "source": "runbook_dummy_prod_api.md",
            "service": "dummy_prod_api",
            "incident_type": "deadlock_memory_health_failure",
            "action": "restart_container"
        }
    }
]


def initialize_vector_db():
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


def inject_runbooks(vector_db):
    """Injects runbook documents into persistent Chroma collection."""
    try:
        texts = [doc["content"] for doc in RUNBOOK_DOCUMENTS]
        metadatas = [doc["metadata"] for doc in RUNBOOK_DOCUMENTS]
        ids = [f"runbook_{doc['metadata']['service']}_{i}" for i, doc in enumerate(RUNBOOK_DOCUMENTS)]

        # Check existing to prevent duplicate insertions
        existing = vector_db.get(ids=ids)
        if existing and existing.get("ids"):
            logger.info("Runbook documents already present in ChromaDB. Updating/Refreshing...")
            vector_db.delete(ids=ids)

        vector_db.add_texts(texts=texts, metadatas=metadatas, ids=ids)
        logger.info("Successfully indexed %d runbook document(s)", len(texts))
    except Exception as exc:
        logger.exception("Failed to inject runbooks: %s", exc)
        raise


def verify_runbook_retrieval(vector_db):
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


def main():
    """Entry point to setup and verify persistent runbooks vector database."""
    logger.info("Starting runbook vector DB setup at '%s'...", CHROMA_PATH)
    vdb = initialize_vector_db()
    inject_runbooks(vdb)
    verify_runbook_retrieval(vdb)
    logger.info("Runbook setup complete!")


if __name__ == "__main__":
    main()

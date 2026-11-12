import logging
import os
import re
from typing import Any, Dict, List

import chromadb
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("setup_policies")

CHROMA_PATH = "./data/chroma"
COLLECTION_NAME = "company_policies"
POLICY_FILE = "policies/company_policy.md"
_POLICY_ID_RE = re.compile(r"POL-\d+")


def load_policy_sections(path: str = POLICY_FILE) -> List[Dict[str, Any]]:
    """Splits the policy markdown on '## ' headings into documents (preamble is skipped)."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    sections: List[Dict[str, Any]] = []
    for block in re.split(r"(?m)^(?=## )", text):
        if not block.startswith("## "):
            continue
        heading = block.splitlines()[0][3:].strip()
        match = _POLICY_ID_RE.search(heading)
        policy_id = match.group(0) if match else f"POL-X{len(sections) + 1}"
        title = heading.split(":", 1)[1].strip() if ":" in heading else heading
        sections.append({
            "content": block.strip(),
            "metadata": {"source": os.path.basename(path), "policy_id": policy_id, "title": title},
        })
    if not sections:
        raise RuntimeError(f"No '## ' policy sections found in {path}")
    return sections


def initialize_vector_db() -> Chroma:
    """Initializes the persistent Chroma collection for policies (local HuggingFace embeddings)."""
    try:
        os.makedirs(CHROMA_PATH, exist_ok=True)
        embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        return Chroma(client=client, collection_name=COLLECTION_NAME, embedding_function=embeddings)
    except Exception as exc:
        logger.exception("Failed to initialize vector database: %s", exc)
        raise


def inject_policies(vector_db: Chroma, sections: List[Dict[str, Any]]) -> None:
    """Replaces all policy documents (idempotent: old ids are deleted before re-adding)."""
    try:
        texts = [s["content"] for s in sections]
        metadatas = [s["metadata"] for s in sections]
        ids = [f"policy_{s['metadata']['policy_id']}" for s in sections]

        existing = vector_db.get()
        old_ids = (existing or {}).get("ids") or []
        if old_ids:
            logger.info("Removing %d existing policy document(s)...", len(old_ids))
            vector_db.delete(ids=old_ids)

        vector_db.add_texts(texts=texts, metadatas=metadatas, ids=ids)
        logger.info("Successfully indexed %d policy document(s)", len(texts))
    except Exception as exc:
        logger.exception("Failed to inject policies: %s", exc)
        raise


def verify_policy_retrieval(vector_db: Chroma) -> None:
    """Verifies retrieval with a sample similarity search."""
    try:
        results = vector_db.similarity_search("EXEC rm", k=2)
        if not results:
            raise RuntimeError("Verification failed: no matching policy retrieved.")
        logger.info("Verification passed! Top policy matches:")
        for doc in results:
            logger.info("%s | %s", doc.metadata.get("policy_id"), doc.metadata.get("title"))
    except Exception as exc:
        logger.exception("Verification query failed: %s", exc)
        raise


def main() -> None:
    """Entry point: index the company policy and verify retrieval."""
    logger.info("Starting policy vector DB setup at '%s' (collection '%s')...", CHROMA_PATH, COLLECTION_NAME)
    sections = load_policy_sections()
    vdb = initialize_vector_db()
    inject_policies(vdb, sections)
    verify_policy_retrieval(vdb)
    logger.info("Policy setup complete!")


if __name__ == "__main__":
    main()

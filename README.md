# VERIFIABLE RAG — PDF RAG QA with Claim-Level Hallucination Verification

A simple, full-stack implementation of the PIER-QA idea plus the extended
feature you described: after the system generates an answer from a PDF,
it splits the answer into individual factual claims, checks each one
against the retrieved evidence, re-retrieves and regenerates if a claim
isn't supported, and abstains rather than guessing if it still can't
find enough evidence.

```
Generate → Claim Extraction → Evidence Verification → Re-retrieval → Regeneration / Abstention
```

**Stack** — no complex billing, no cloud infrastructure required:
- **Backend:** Python + FastAPI
- **Embeddings:** `sentence-transformers` (local, free, runs on your machine)
- **Vector store:** ChromaDB (local file-based, free)
- **LLM:** any OpenAI-API-compatible provider — Groq's free tier 
- **Frontend:** plain HTML/CSS/JS (no build step, no npm required)

---

## 1. Project structure

```
pier-qa/
  backend/
    app.py                  # FastAPI app (routes: /upload, /ask, /documents)
    modules/
      pdf_processor.py      # PDF -> text -> chunks
      embedder.py           # local embedding model
      vectorstore.py        # ChromaDB wrapper
      llm_client.py         # OpenAI-compatible LLM wrapper
      claim_verifier.py     # the extended feature: verification loop
    requirements.txt
    .env.example            # copy to .env and fill in your LLM key
  frontend/
    index.html
    style.css
    script.js
```

## 2. Setup in VS Code

1. Open the `pier-qa/` folder in VS Code.
2. Open a terminal (`` Ctrl+` ``) and set up the backend:

   ```bash
   cd backend
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

3. Get a free LLM API key — **Groq is the simplest, no billing at all**:
   - Go to https://console.groq.com/keys, sign up, create a key.
   - Paste it into `backend/.env` as `LLM_API_KEY=...`.
   - (The `.env.example` file also shows how to use OpenAI or a fully
     local Ollama model instead, if you'd rather not use any API key.)

4. Start the backend:

   ```bash
   uvicorn app:app --reload --port 8000
   ```

   First run will download the small local embedding model
   (~90MB, one-time). Leave this terminal running.

5. Open the frontend: in VS Code, right-click `frontend/index.html` →
   **"Open with Live Server"** (install the free "Live Server" extension
   if you don't have it), or just double-click `index.html` to open it
   in your browser directly — no build step needed.

## 3. Using it

1. Upload a PDF (your research paper, a report, anything text-based).
2. Ask a question about it.
3. You'll see:
   - **The final, verified answer**
   - **Claim verification** — each factual claim in the answer, labeled
     `SUPPORTED`, `CONTRADICTED`, or `UNSUPPORTED` against the retrieved
     text, with a short reason
   - **Sources used** — the exact chunks retrieved from your PDF

If a claim can't be verified even after a second, targeted retrieval
round, the system abstains instead of presenting an unverified answer.

## 4. Notes & extension points

To keep this a simple, reliably-running reference build, a few things
from the base paper are intentionally left out — they're easy to add
later without changing the architecture:
- Header/footer removal (the paper uses DBSCAN over element bounding boxes)
- Table extraction into structured chunks (e.g. via `pdfplumber`)
- Image extraction + captioning for image-based Q&A

The claim-verification module (`claim_verifier.py`) is the core of the
extended feature — that's the file to read first and the one you'd
extend if you want more correction rounds, different verdict categories,
or a different verification prompt.

## 5. Troubleshooting

- **"Connection refused" in the frontend** → make sure `uvicorn` is
  still running in the terminal.
- **401/403 from the LLM** → double check `LLM_API_KEY` in `.env` and
  that `LLM_BASE_URL`/`LLM_MODEL` match the provider you picked.
- **Slow first upload** → the embedding model downloads once, then is
  cached locally.

# ABCD Detector API (Backend)

Phase 1 + 2: FastAPI backend with job create/status/result. **Mock** analysis when GCP is not configured; **real ABCD** (YouTube + GCS) when `GCP_PROJECT_ID` is set in `.env`. Run locally first; deployment later.

## Quick start (local)

### 1. Create virtual environment and install deps

From the **backend** directory:

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 2. Optional: environment

Copy `.env.example` to `.env` and change if needed (defaults work for local):

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

### 3. Run the API

From the **backend** directory:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- API: **http://localhost:8000**
- Docs: **http://localhost:8000/docs**
- Health: **http://localhost:8000/health**

## API (Phase 1)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| POST | `/jobs` | Create analysis job (body: `youtube_url` or `video_url`, optional `brand_name`) |
| GET | `/jobs/{job_id}` | Get job status and result |
| GET | `/jobs` | List recent jobs (`?limit=20`) |

### Example: create job and poll

```bash
# Create job
curl -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -d "{\"youtube_url\": \"https://www.youtube.com/watch?v=xyz\", \"brand_name\": \"Test Brand\"}"

# Get status (use job_id from response)
curl http://localhost:8000/jobs/<job_id>
```

- **Without GCP:** After ~2s the job returns `status: "completed"` with a **mock** result.
- **With GCP:** Set `GCP_PROJECT_ID` (and optionally `GCS_BUCKET` for uploads) in `.env`. Jobs run the **real** ABCD detector (YouTube URL or `gs://` URI). Analysis may take several minutes.

## Phase 2 – Real ABCD

1. Clone/copy of upstream ABCD is in `backend/abcd_original/` (already done).
2. Set in `.env`: `GCP_PROJECT_ID`, `GCP_REGION` (e.g. `us-central1`). For file uploads you also need `GCS_BUCKET`.
3. Enable Vertex AI and (for GCS) Video Intelligence and Cloud Storage in your GCP project. For YouTube-only, LLM (Gemini) is used.
4. Optional: set `USE_REAL_ABCD=false` to force mock even when GCP is set.

## Project layout

```
backend/
├── main.py           # FastAPI app, CORS, routes
├── config.py         # Settings from env
├── schemas.py        # Pydantic + in-memory job store (named to avoid clash with ABCD models)
├── abcd_service.py   # Phase 2: run real ABCD (YouTube / GCS)
├── abcd_original/    # Cloned upstream ABCD detector
├── routers/
│   └── jobs.py       # POST/GET /jobs
├── requirements.txt
├── .env.example
└── README.md         # this file
```

## References

- **API contract and ABCD references:** `../docs/REFERENCE.md`
- **Future enhancements and go-to-market:** `../docs/FUTURE_ENHANCEMENTS.md`
- **Development phases:** `../DEVELOPMENT_PLAN.md`

## Next (Phase 3+)

- Frontend (submit job, poll, view results). Replace in-memory store with DB for production.

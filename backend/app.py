"""FastAPI backend for the HaaS security dashboard.

Endpoints:
  GET /health            - backend + AWS connectivity
  GET /api/stats         - aggregate KPIs
  GET /api/events        - recent events (paginated)
  GET /api/charts        - events over time + top IPs
  GET /api/pipeline      - best-effort pipeline health

All data comes from the real CowrieLogs DynamoDB table. If an AWS service
is unavailable, endpoints return a clear error rather than mock data.
"""

import time
import re
import uuid

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import dynamo, probes
from .aws import client as aws_client
from .cowrie import TooManyCowrieEvents, expected_events
from .config import (
    AWS_MODE, AWS_REGION, AWS_ENDPOINT_URL, HAA_DYNAMODB_TABLE,
    HAA_S3_BUCKET, DATA_SOURCE_LABEL,
)

# Bound memory and limit dashboard uploads to one initial DynamoDB batch
# (AWS accepts up to 25 items per BatchWriteItem; SDK retries may still occur).
MAX_UPLOAD_BYTES = 16 * 1024
MAX_UPLOAD_EVENTS = 25
UPLOAD_KEY_PREFIX = "uploads"
UPLOAD_STATUS_TTL_SECONDS = 3600
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,123}\.log$", re.IGNORECASE)
UPLOAD_EXPECTED = {}

app = FastAPI(
    title="AWS-CloudX-HoneyPot Dashboard API",
    version="1.0.0",
    description="Security dashboard backend for the HaaS pipeline.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Backend + AWS connectivity check."""
    start = time.time()
    table_ok = dynamo.table_exists()
    latency_ms = int((time.time() - start) * 1000)
    return {
        "status": "ok" if table_ok else "degraded",
        "backend": "ok",
        "mode": AWS_MODE,
        "region": AWS_REGION,
        "endpoint": AWS_ENDPOINT_URL,
        "table": HAA_DYNAMODB_TABLE,
        "table_reachable": table_ok,
        "latency_ms": latency_ms,
        "data_source": DATA_SOURCE_LABEL,
    }


@app.get("/api/stats")
async def stats():
    """Aggregate KPIs over a bounded scan of CowrieLogs."""
    try:
        return dynamo.stats()
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Unable to query DynamoDB: {e}",
        )


@app.get("/api/events")
async def events(
    limit: int = Query(default=50, ge=1, le=500),
    src_ip: str = Query(default=None),
    session: str = Query(default=None),
):
    """Recent events, optionally filtered by src_ip or session."""
    try:
        if session:
            items = dynamo.query_by_session(session, limit=limit)
        elif src_ip:
            items = dynamo.query_by_ip(src_ip, limit=limit)
        else:
            items = dynamo.query_recent(limit=limit)
        return {
            "events": items,
            "count": len(items),
            "limit": limit,
            "data_source": DATA_SOURCE_LABEL,
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Unable to query DynamoDB: {e}",
        )


@app.get("/api/charts")
async def charts(
    limit: int = Query(default=12, ge=1, le=100),
):
    """Events-over-time buckets and top source IPs."""
    try:
        return {
            "events_over_time": dynamo.events_over_time(limit_buckets=limit),
            "top_ips": dynamo.top_ips(limit=10),
            "data_source": DATA_SOURCE_LABEL,
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Unable to query DynamoDB: {e}",
        )


@app.get("/api/pipeline")
async def pipeline():
    """Best-effort pipeline health. Never raises - always returns status."""
    try:
        return probes.pipeline_status()
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"mode": AWS_MODE, "error": str(e)},
        )


@app.get("/api/config")
async def config():
    """Public config for the frontend (no secrets)."""
    return {
        "mode": AWS_MODE,
        "region": AWS_REGION,
        "endpoint": AWS_ENDPOINT_URL,
        "table": HAA_DYNAMODB_TABLE,
        "data_source": DATA_SOURCE_LABEL,
    }


@app.post("/api/uploads", status_code=202)
async def upload_log(file: UploadFile = File(...)):
    """Upload a Cowrie .log file; the existing S3 trigger handles parsing."""
    filename = file.filename or ""
    if (
        not UPLOAD_FILENAME_PATTERN.fullmatch(filename)
        or ".." in filename
        or "/" in filename
        or "\\" in filename
    ):
        raise HTTPException(status_code=400, detail="Choose a valid .log filename.")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Log file exceeds the 16 KiB limit.")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Log file is empty.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Log file must be UTF-8 text.")
    if "\x00" in text:
        raise HTTPException(status_code=400, detail="Log file contains invalid text data.")
    try:
        expected = expected_events(text, max_events=MAX_UPLOAD_EVENTS)
    except TooManyCowrieEvents:
        raise HTTPException(
            status_code=413,
            detail=f"Log file exceeds the {MAX_UPLOAD_EVENTS}-event limit.",
        )
    if not expected:
        raise HTTPException(
            status_code=400,
            detail=(
                "No valid Cowrie events were found. Include timestamped records and a "
                "connection line matching 'New connection: <source-ip>:<port> "
                "(<destination-ip>:<port>) [session: <id>]'."
            ),
        )

    upload_id = uuid.uuid4().hex
    object_key = f"{UPLOAD_KEY_PREFIX}/{upload_id}.log"
    try:
        aws_client("s3").put_object(
            Bucket=HAA_S3_BUCKET,
            Key=object_key,
            Body=content,
            ContentType="text/plain; charset=utf-8",
            Metadata={"original-filename": filename},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="S3 upload failed.") from exc

    now = time.monotonic()
    for expired_id, (created, _) in list(UPLOAD_EXPECTED.items()):
        if now - created > UPLOAD_STATUS_TTL_SECONDS:
            UPLOAD_EXPECTED.pop(expired_id, None)
    UPLOAD_EXPECTED[upload_id] = (now, expected)
    return {"upload_id": upload_id, "status": "uploaded", "filename": filename}


@app.get("/api/uploads/{upload_id}")
async def upload_status(upload_id: str):
    """Confirm that parser output for the upload is visible in DynamoDB."""
    if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise HTTPException(status_code=404, detail="Upload not found.")
    upload = UPLOAD_EXPECTED.get(upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload status is no longer available.")
    created, expected = upload
    if time.monotonic() - created > UPLOAD_STATUS_TTL_SECONDS:
        UPLOAD_EXPECTED.pop(upload_id, None)
        raise HTTPException(status_code=404, detail="Upload status has expired.")
    object_key = f"{UPLOAD_KEY_PREFIX}/{upload_id}.log"
    try:
        found = dynamo.upload_events(object_key, expected)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to check processing status.") from exc
    if expected.issubset(found):
        return {"upload_id": upload_id, "status": "processed", "events_detected": len(found)}
    return {"upload_id": upload_id, "status": "processing", "events_detected": len(found)}

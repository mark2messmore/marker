"""
Enhanced Marker server with UI support.
Features: Queue system, page-based chunking, SSE progress, job history, persistent settings.
"""
import asyncio
import gc
import json
import os
import sqlite3
import traceback
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Set

import torch

import click
import uvicorn
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.output import text_from_rendered
from marker.settings import settings as marker_settings

# API Router with /api prefix
api_router = APIRouter(prefix="/api")

# Directories
BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data" / "ui_data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "marker_ui.db"

# Ensure directories exist
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Constants
MAX_PAGES_PER_CHUNK = 5  # Process 5 pages at a time to avoid GPU OOM

# Global state
app_data = {}
queue_state = {
    "jobs": deque(),  # Queue of job dicts
    "current_job": None,  # Currently processing job
    "sse_clients": set(),  # Connected SSE clients
    "processing": False,  # Is queue processor running
}


def get_table_columns(conn, table_name: str) -> set:
    """Get existing column names for a table."""
    c = conn.cursor()
    c.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in c.fetchall()}


def add_column_if_missing(conn, table_name: str, column_name: str, column_def: str):
    """Add a column to a table if it doesn't exist."""
    existing_columns = get_table_columns(conn, table_name)
    if column_name not in existing_columns:
        c = conn.cursor()
        c.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
        print(f"  Added column '{column_name}' to '{table_name}'")


def init_db():
    """Initialize SQLite database with migration support."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("Initializing database...")

    # Create tables if they don't exist
    c.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS queue (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    conn.commit()

    # Migrate: Add missing columns to 'jobs' table
    print("Checking for schema migrations...")

    jobs_migrations = [
        ("file_size", "INTEGER DEFAULT 0"),
        ("completed_at", "TEXT"),
        ("has_markdown", "INTEGER DEFAULT 0"),
        ("has_json", "INTEGER DEFAULT 0"),
        ("has_images", "INTEGER DEFAULT 0"),
        ("error_message", "TEXT"),
        ("total_chunks", "INTEGER DEFAULT 1"),
        ("current_chunk", "INTEGER DEFAULT 0"),
        ("percent", "INTEGER DEFAULT 0"),
        ("message", "TEXT"),
    ]

    for col_name, col_def in jobs_migrations:
        add_column_if_missing(conn, "jobs", col_name, col_def)

    # Migrate: Add missing columns to 'queue' table
    queue_migrations = [
        ("file_size", "INTEGER DEFAULT 0"),
        ("output_markdown", "INTEGER DEFAULT 1"),
        ("output_json", "INTEGER DEFAULT 0"),
        ("output_images", "INTEGER DEFAULT 1"),
        ("force_ocr", "INTEGER DEFAULT 0"),
        ("paginate_output", "INTEGER DEFAULT 0"),
        ("position", "INTEGER DEFAULT 0"),
    ]

    for col_name, col_def in queue_migrations:
        add_column_if_missing(conn, "queue", col_name, col_def)

    conn.commit()
    conn.close()
    print("Database ready!")


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_pdf_info(filepath: str) -> dict:
    """Get PDF page count and file size."""
    import pypdfium2 as pdfium

    file_size = os.path.getsize(filepath)
    doc = pdfium.PdfDocument(filepath)
    page_count = len(doc)
    doc.close()

    return {"page_count": page_count, "file_size": file_size}


def calculate_chunks(page_count: int, file_size: int = 0) -> List[List[int]]:
    """
    Calculate page ranges for processing in small chunks.
    Returns list of page ranges, e.g. [[0,1,2,3,4], [5,6,7,8,9], ...]

    Uses fixed pages per chunk to avoid GPU OOM errors.
    """
    if page_count <= MAX_PAGES_PER_CHUNK:
        # Small file, process all at once
        return [list(range(page_count))]

    chunks = []
    for start in range(0, page_count, MAX_PAGES_PER_CHUNK):
        end = min(start + MAX_PAGES_PER_CHUNK, page_count)
        chunks.append(list(range(start, end)))

    return chunks


async def broadcast_queue_update():
    """Send queue update to all connected SSE clients."""
    queue_data = get_queue_data()
    message = json.dumps({"type": "queue_update", "queue": queue_data})

    dead_clients = set()
    for client_queue in queue_state["sse_clients"]:
        try:
            await client_queue.put(message)
        except:
            dead_clients.add(client_queue)

    # Remove dead clients
    queue_state["sse_clients"] -= dead_clients


def get_queue_data() -> List[dict]:
    """Get current queue state for frontend."""
    result = []

    # Add current job if processing
    if queue_state["current_job"]:
        job = queue_state["current_job"]
        result.append({
            "id": job["id"],
            "filename": job["filename"],
            "file_size": job["file_size"],
            "status": "processing",
            "total_chunks": job.get("total_chunks", 1),
            "current_chunk": job.get("current_chunk", 1),
            "percent": job.get("percent", 0),
            "message": job.get("message", "Processing..."),
        })

    # Add queued jobs
    for job in queue_state["jobs"]:
        result.append({
            "id": job["id"],
            "filename": job["filename"],
            "file_size": job["file_size"],
            "status": "queued",
            "total_chunks": 1,
            "current_chunk": 0,
            "percent": 0,
            "message": "Waiting in queue...",
        })

    return result


async def process_queue():
    """Main queue processor - runs one job at a time."""
    if queue_state["processing"]:
        return

    queue_state["processing"] = True

    try:
        while queue_state["jobs"]:
            # Get next job
            job = queue_state["jobs"].popleft()
            queue_state["current_job"] = job

            await broadcast_queue_update()

            try:
                await process_job(job)
            except Exception as e:
                traceback.print_exc()
                # Mark job as error in history
                save_job_to_history(job, "error", str(e))

            queue_state["current_job"] = None

            # Notify clients job is complete
            for client_queue in queue_state["sse_clients"]:
                try:
                    await client_queue.put(json.dumps({"type": "job_complete", "job_id": job["id"]}))
                except:
                    pass

            await broadcast_queue_update()
    finally:
        queue_state["processing"] = False


async def update_job_progress(job: dict, message: str, percent: int, current_chunk: int = None):
    """Update job progress and broadcast to clients."""
    job["message"] = message
    job["percent"] = percent
    if current_chunk is not None:
        job["current_chunk"] = current_chunk

    await broadcast_queue_update()


async def process_job(job: dict):
    """Process a single job with chunking support."""
    filepath = Path(job["filepath"])
    job_id = job["id"]

    # Get PDF info
    pdf_info = get_pdf_info(str(filepath))
    page_count = pdf_info["page_count"]
    file_size = pdf_info["file_size"]

    # Calculate chunks
    chunks = calculate_chunks(page_count, file_size)
    total_chunks = len(chunks)
    job["total_chunks"] = total_chunks
    job["current_chunk"] = 0

    await update_job_progress(job, f"Starting conversion ({total_chunks} chunk{'s' if total_chunks > 1 else ''})...", 5)

    # Create output directory
    job_output_dir = OUTPUT_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    # Store chunk results for merging
    chunk_results = []

    for chunk_idx, page_range in enumerate(chunks):
        job["current_chunk"] = chunk_idx + 1
        chunk_start_percent = int((chunk_idx / total_chunks) * 80) + 10
        chunk_end_percent = int(((chunk_idx + 1) / total_chunks) * 80) + 10

        await update_job_progress(
            job,
            f"Processing pages {page_range[0]+1}-{page_range[-1]+1}...",
            chunk_start_percent,
            chunk_idx + 1
        )

        # Configure conversion for this chunk
        # Note: Don't pass page_range to ConfigParser - it expects a string
        # We set it directly in config_dict as a list
        options = {
            "force_ocr": job["force_ocr"],
            "paginate_output": job["paginate_output"],
            "output_format": "markdown"
        }

        config_parser = ConfigParser(options)
        config_dict = config_parser.generate_config_dict()
        config_dict["pdftext_workers"] = 1
        config_dict["page_range"] = page_range  # Set page_range directly as list

        converter = PdfConverter(
            config=config_dict,
            artifact_dict=app_data["models"],
            processor_list=config_parser.get_processors(),
            renderer=config_parser.get_renderer(),
            llm_service=config_parser.get_llm_service(),
        )

        # Run conversion in thread pool
        rendered = await asyncio.to_thread(converter, str(filepath))

        text, _, images = text_from_rendered(rendered)
        metadata = rendered.metadata

        chunk_results.append({
            "text": text,
            "images": images,
            "metadata": metadata,
            "page_range": page_range,
        })

        # Clean up memory after each chunk to prevent OOM
        del converter
        del rendered
        del config_parser
        del config_dict
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        await update_job_progress(job, f"Chunk {chunk_idx + 1}/{total_chunks} complete", chunk_end_percent)

    # Merge results
    await update_job_progress(job, "Merging chunks...", 90)

    merged_text, merged_images, merged_metadata = merge_chunk_results(chunk_results)

    # Save outputs
    base_name = filepath.stem
    has_markdown = False
    has_json = False
    has_images = False

    if job["output_markdown"]:
        await update_job_progress(job, "Saving markdown...", 92)
        md_path = job_output_dir / f"{base_name}.md"
        md_path.write_text(merged_text, encoding="utf-8")
        has_markdown = True

    if job["output_json"]:
        await update_job_progress(job, "Saving JSON...", 95)
        json_path = job_output_dir / f"{base_name}.json"
        json_data = {
            "text": merged_text,
            "metadata": merged_metadata
        }
        json_path.write_text(json.dumps(json_data, indent=2, default=str), encoding="utf-8")
        has_json = True

    if job["output_images"] and merged_images:
        await update_job_progress(job, "Saving images...", 97)
        images_dir = job_output_dir / "images"
        images_dir.mkdir(exist_ok=True)
        for img_name, img in merged_images.items():
            img_path = images_dir / img_name
            img.save(img_path, format=marker_settings.OUTPUT_IMAGE_FORMAT)
        has_images = True

    await update_job_progress(job, "Complete!", 100)

    # Save to history
    save_job_to_history(job, "complete", None, has_markdown, has_json, has_images)

    # Clean up uploaded file
    if filepath.exists():
        filepath.unlink()

    # Remove from queue table
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM queue WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()


def merge_chunk_results(chunk_results: List[dict]) -> tuple:
    """Merge multiple chunk results into single output."""
    if len(chunk_results) == 1:
        return (
            chunk_results[0]["text"],
            chunk_results[0]["images"],
            chunk_results[0]["metadata"]
        )

    # Merge text with page separators
    separator = "\n\n" + "-" * 48 + "\n\n"
    merged_text = separator.join(chunk["text"] for chunk in chunk_results)

    # Merge images
    merged_images = {}
    for chunk in chunk_results:
        if chunk["images"]:
            merged_images.update(chunk["images"])

    # Merge metadata
    merged_metadata = {
        "table_of_contents": chunk_results[0]["metadata"].get("table_of_contents", []),
        "chunk_count": len(chunk_results),
        "page_stats": [],
    }
    for chunk in chunk_results:
        if "page_stats" in chunk["metadata"]:
            merged_metadata["page_stats"].extend(chunk["metadata"]["page_stats"])

    return merged_text, merged_images, merged_metadata


def save_job_to_history(job: dict, status: str, error_message: str = None,
                        has_markdown: bool = False, has_json: bool = False, has_images: bool = False):
    """Save completed job to history database."""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        INSERT OR REPLACE INTO jobs
        (id, filename, file_size, status, created_at, completed_at,
         has_markdown, has_json, has_images, error_message, total_chunks)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job["id"],
        job["filename"],
        job["file_size"],
        status,
        job["created_at"],
        datetime.now().isoformat(),
        has_markdown,
        has_json,
        has_images,
        error_message,
        job.get("total_chunks", 1)
    ))

    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load models on startup."""
    init_db()
    print("Loading marker models...")
    app_data["models"] = create_model_dict()
    print("Models loaded!")
    yield
    if "models" in app_data:
        del app_data["models"]


app = FastAPI(lifespan=lifespan)

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Settings(BaseModel):
    outputMarkdown: bool = True
    outputJson: bool = False
    outputImages: bool = True
    forceOcr: bool = False
    paginateOutput: bool = False


# --- Settings Endpoints ---

@api_router.get("/settings")
async def get_settings():
    """Get saved settings."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    rows = c.fetchall()
    conn.close()

    settings_dict = {row["key"]: json.loads(row["value"]) for row in rows}
    return settings_dict


@api_router.post("/settings")
async def save_settings(settings: Settings):
    """Save settings."""
    conn = get_db()
    c = conn.cursor()

    for key, value in settings.model_dump().items():
        c.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value))
        )

    conn.commit()
    conn.close()
    return {"status": "ok"}


# --- Queue Endpoints ---

@api_router.get("/queue")
async def get_queue():
    """Get current queue."""
    return get_queue_data()


@api_router.post("/queue/add")
async def add_to_queue(
    file: UploadFile = File(...),
    output_markdown: bool = Form(True),
    output_json: bool = Form(False),
    output_images: bool = Form(True),
    force_ocr: bool = Form(False),
    paginate_output: bool = Form(False)
):
    """Add a file to the queue."""
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Save uploaded file
    upload_path = UPLOAD_DIR / f"{job_id}.pdf"
    content = await file.read()
    with open(upload_path, "wb") as f:
        f.write(content)

    file_size = len(content)

    # Create job dict
    job = {
        "id": job_id,
        "filename": file.filename,
        "file_size": file_size,
        "filepath": str(upload_path),
        "output_markdown": output_markdown,
        "output_json": output_json,
        "output_images": output_images,
        "force_ocr": force_ocr,
        "paginate_output": paginate_output,
        "created_at": datetime.now().isoformat(),
    }

    # Add to queue
    queue_state["jobs"].append(job)

    # Save to queue table for persistence
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO queue (id, filename, file_size, filepath, output_markdown,
                          output_json, output_images, force_ocr, paginate_output, created_at, position)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id, file.filename, file_size, str(upload_path),
        output_markdown, output_json, output_images, force_ocr, paginate_output,
        job["created_at"], len(queue_state["jobs"])
    ))
    conn.commit()
    conn.close()

    await broadcast_queue_update()

    # Start processing if not already running
    asyncio.create_task(process_queue())

    return {"job_id": job_id, "position": len(queue_state["jobs"])}


@api_router.delete("/queue/{job_id}")
async def remove_from_queue(job_id: str):
    """Remove a job from queue (only if not processing)."""
    # Find and remove from queue
    for i, job in enumerate(queue_state["jobs"]):
        if job["id"] == job_id:
            queue_state["jobs"].remove(job)

            # Delete uploaded file
            filepath = Path(job["filepath"])
            if filepath.exists():
                filepath.unlink()

            # Remove from database
            conn = get_db()
            c = conn.cursor()
            c.execute("DELETE FROM queue WHERE id = ?", (job_id,))
            conn.commit()
            conn.close()

            await broadcast_queue_update()
            return {"status": "ok"}

    raise HTTPException(status_code=404, detail="Job not found in queue")


@api_router.get("/queue/stream")
async def queue_stream():
    """SSE endpoint for queue updates."""
    client_queue = asyncio.Queue()
    queue_state["sse_clients"].add(client_queue)

    async def event_generator():
        try:
            # Send initial state
            yield {"data": json.dumps({"type": "queue_update", "queue": get_queue_data()})}

            while True:
                try:
                    message = await asyncio.wait_for(client_queue.get(), timeout=30)
                    yield {"data": message}
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {"data": json.dumps({"type": "keepalive"})}
        finally:
            queue_state["sse_clients"].discard(client_queue)

    return EventSourceResponse(event_generator())


# --- History Endpoints ---

@api_router.get("/history")
async def get_history():
    """Get conversion history."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, filename, file_size, status, created_at, completed_at,
               has_markdown, has_json, has_images, error_message, total_chunks
        FROM jobs
        ORDER BY created_at DESC
        LIMIT 50
    """)
    rows = c.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# --- Download Endpoints ---

@api_router.get("/download/{job_id}/markdown")
async def download_markdown(job_id: str):
    """Download markdown file."""
    job_dir = OUTPUT_DIR / job_id
    md_files = list(job_dir.glob("*.md"))

    if not md_files:
        raise HTTPException(status_code=404, detail="Markdown file not found")

    return FileResponse(
        md_files[0],
        media_type="text/markdown",
        filename=md_files[0].name
    )


@api_router.get("/download/{job_id}/json")
async def download_json(job_id: str):
    """Download JSON file."""
    job_dir = OUTPUT_DIR / job_id
    json_files = list(job_dir.glob("*.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="JSON file not found")

    return FileResponse(
        json_files[0],
        media_type="application/json",
        filename=json_files[0].name
    )


@api_router.get("/download/{job_id}/images")
async def download_images(job_id: str):
    """Download images as ZIP."""
    images_dir = OUTPUT_DIR / job_id / "images"

    if not images_dir.exists() or not list(images_dir.iterdir()):
        raise HTTPException(status_code=404, detail="No images found")

    # Create ZIP in memory
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for img_path in images_dir.iterdir():
            zf.write(img_path, img_path.name)

    zip_buffer.seek(0)

    # Get original filename from job
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT filename FROM jobs WHERE id = ?", (job_id,))
    row = c.fetchone()
    conn.close()

    filename = f"{Path(row['filename']).stem}_images.zip" if row else "images.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# Include API router
app.include_router(api_router)


# --- Static Files (serve built Svelte app) ---

def setup_static_files(app: FastAPI):
    """Mount static files if build exists."""
    static_dir = BASE_DIR / "ui" / "build"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


@click.command()
@click.option("--port", type=int, default=8000, help="Port to run the server on")
@click.option("--host", type=str, default="0.0.0.0", help="Host to run the server on")
def server_ui_cli(port: int, host: str):
    """Run Marker server with UI support."""
    setup_static_files(app)

    print(f"\n{'='*50}")
    print(f"  Marker UI Server")
    print(f"  http://{host}:{port}")
    print(f"  Max pages per chunk: {MAX_PAGES_PER_CHUNK}")
    print(f"{'='*50}\n")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    server_ui_cli()

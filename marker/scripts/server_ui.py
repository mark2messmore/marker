"""
Enhanced Marker server with UI support.
Features: SSE progress, job history, persistent settings, file downloads.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import traceback
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import click
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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

# Directories
BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data" / "ui_data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "marker_ui.db"

# Ensure directories exist
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Global state
app_data = {}
job_progress = {}  # job_id -> progress dict


def init_db():
    """Initialize SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            has_markdown INTEGER DEFAULT 0,
            has_json INTEGER DEFAULT 0,
            has_images INTEGER DEFAULT 0,
            error_message TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

@app.get("/settings")
async def get_settings():
    """Get saved settings."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    rows = c.fetchall()
    conn.close()

    settings_dict = {row["key"]: json.loads(row["value"]) for row in rows}
    return settings_dict


@app.post("/settings")
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


# --- History Endpoints ---

@app.get("/history")
async def get_history():
    """Get conversion history."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, filename, status, created_at, completed_at,
               has_markdown, has_json, has_images, error_message
        FROM jobs
        ORDER BY created_at DESC
        LIMIT 50
    """)
    rows = c.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# --- Conversion Endpoints ---

def update_progress(job_id: str, status: str, message: str, percent: int):
    """Update job progress."""
    job_progress[job_id] = {
        "status": status,
        "message": message,
        "percent": percent
    }


async def convert_pdf_task(
    job_id: str,
    filepath: Path,
    output_markdown: bool,
    output_json: bool,
    output_images: bool,
    force_ocr: bool,
    paginate_output: bool
):
    """Background task for PDF conversion."""
    job_output_dir = OUTPUT_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    c = conn.cursor()

    try:
        update_progress(job_id, "processing", "Initializing converter...", 10)
        await asyncio.sleep(0.1)  # Allow SSE to catch up

        # Configure conversion
        options = {
            "force_ocr": force_ocr,
            "paginate_output": paginate_output,
            "output_format": "markdown"
        }

        config_parser = ConfigParser(options)
        config_dict = config_parser.generate_config_dict()
        config_dict["pdftext_workers"] = 1

        update_progress(job_id, "processing", "Loading PDF...", 20)
        await asyncio.sleep(0.1)

        converter = PdfConverter(
            config=config_dict,
            artifact_dict=app_data["models"],
            processor_list=config_parser.get_processors(),
            renderer=config_parser.get_renderer(),
            llm_service=config_parser.get_llm_service(),
        )

        update_progress(job_id, "processing", "Converting document...", 30)
        await asyncio.sleep(0.1)

        # Run conversion (this is the slow part)
        rendered = await asyncio.to_thread(converter, str(filepath))

        update_progress(job_id, "processing", "Extracting content...", 70)
        await asyncio.sleep(0.1)

        text, _, images = text_from_rendered(rendered)
        metadata = rendered.metadata

        has_markdown = False
        has_json = False
        has_images = False

        # Save outputs
        base_name = filepath.stem

        if output_markdown:
            update_progress(job_id, "processing", "Saving markdown...", 80)
            md_path = job_output_dir / f"{base_name}.md"
            md_path.write_text(text, encoding="utf-8")
            has_markdown = True

        if output_json:
            update_progress(job_id, "processing", "Saving JSON...", 85)
            json_path = job_output_dir / f"{base_name}.json"
            json_data = {
                "text": text,
                "metadata": metadata
            }
            json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
            has_json = True

        if output_images and images:
            update_progress(job_id, "processing", "Saving images...", 90)
            images_dir = job_output_dir / "images"
            images_dir.mkdir(exist_ok=True)
            for img_name, img in images.items():
                img_path = images_dir / img_name
                img.save(img_path, format=marker_settings.OUTPUT_IMAGE_FORMAT)
            has_images = True

        # Update database
        c.execute("""
            UPDATE jobs
            SET status = 'complete',
                completed_at = ?,
                has_markdown = ?,
                has_json = ?,
                has_images = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), has_markdown, has_json, has_images, job_id))
        conn.commit()

        update_progress(job_id, "complete", "Conversion complete!", 100)

    except Exception as e:
        traceback.print_exc()
        error_msg = str(e)
        c.execute("""
            UPDATE jobs
            SET status = 'error',
                completed_at = ?,
                error_message = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), error_msg, job_id))
        conn.commit()
        update_progress(job_id, "error", f"Error: {error_msg}", 0)

    finally:
        conn.close()
        # Clean up uploaded file
        if filepath.exists():
            filepath.unlink()


@app.post("/convert")
async def start_conversion(
    file: UploadFile = File(...),
    output_markdown: bool = Form(True),
    output_json: bool = Form(False),
    output_images: bool = Form(True),
    force_ocr: bool = Form(False),
    paginate_output: bool = Form(False)
):
    """Start a PDF conversion job."""
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Save uploaded file
    upload_path = UPLOAD_DIR / f"{job_id}.pdf"
    with open(upload_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # Create job record
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO jobs (id, filename, status, created_at)
        VALUES (?, ?, 'processing', ?)
    """, (job_id, file.filename, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    # Initialize progress
    job_progress[job_id] = {
        "status": "processing",
        "message": "Starting conversion...",
        "percent": 5
    }

    # Start background task
    asyncio.create_task(convert_pdf_task(
        job_id,
        upload_path,
        output_markdown,
        output_json,
        output_images,
        force_ocr,
        paginate_output
    ))

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """SSE endpoint for job progress."""
    async def event_generator():
        while True:
            if job_id in job_progress:
                progress = job_progress[job_id]
                yield {"data": json.dumps(progress)}

                if progress["status"] in ("complete", "error"):
                    # Clean up progress after final event
                    await asyncio.sleep(1)
                    if job_id in job_progress:
                        del job_progress[job_id]
                    break

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


# --- Download Endpoints ---

@app.get("/download/{job_id}/markdown")
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


@app.get("/download/{job_id}/json")
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


@app.get("/download/{job_id}/images")
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
    print(f"{'='*50}\n")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    server_ui_cli()

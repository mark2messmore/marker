"""
Google Drive integration for Marker.

Polls a Google Drive folder for new files, downloads them for processing,
and uploads completed results back to Drive.

Setup:
1. Create a Google Cloud project
2. Enable the Google Drive API
3. Create a service account and download credentials JSON
4. Share your Drive folders with the service account email
5. Set environment variables (see below)

Environment variables:
- GDRIVE_CREDENTIALS_PATH: Path to service account JSON file
- GDRIVE_UPLOAD_FOLDER_ID: ID of the "Upload" folder in Drive
- GDRIVE_DONE_FOLDER_ID: ID of the "Done" folder in Drive
- GDRIVE_POLL_INTERVAL: Polling interval in seconds (default: 60)
- GDRIVE_ENABLED: Set to "true" to enable (default: false)
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Optional

# Google API imports - these need to be installed
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False


# Supported MIME types for conversion
SUPPORTED_MIME_TYPES = {
    'application/pdf': '.pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/epub+zip': '.epub',
    'text/html': '.html',
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/tiff': '.tiff',
}

# For now, let's focus on PDFs since that's the main use case
PDF_MIME_TYPES = {'application/pdf'}


class GoogleDrivePoller:
    """
    Polls Google Drive for new files and manages the upload/download flow.
    """

    def __init__(
        self,
        credentials_path: str,
        upload_folder_id: str,
        done_folder_id: str,
        db_path: Path,
        local_upload_dir: Path,
        local_output_dir: Path,
    ):
        """
        Initialize the Google Drive poller.

        Args:
            credentials_path: Path to service account JSON credentials
            upload_folder_id: Google Drive folder ID for uploads
            done_folder_id: Google Drive folder ID for completed files
            db_path: Path to SQLite database
            local_upload_dir: Local directory to store downloaded files
            local_output_dir: Local directory where processed outputs are stored
        """
        if not GOOGLE_API_AVAILABLE:
            raise ImportError(
                "Google API libraries not installed. Run: "
                "pip install google-api-python-client google-auth"
            )

        self.upload_folder_id = upload_folder_id
        self.done_folder_id = done_folder_id
        self.db_path = db_path
        self.local_upload_dir = Path(local_upload_dir)
        self.local_output_dir = Path(local_output_dir)

        # Initialize Google Drive service
        self.credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        self.service = build('drive', 'v3', credentials=self.credentials)

        # Initialize tracking database
        self._init_tracking_db()

    def _init_tracking_db(self):
        """Initialize the tracking table for processed Drive files."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS gdrive_processed (
                drive_file_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                job_id TEXT,
                processed_at TEXT NOT NULL,
                uploaded_to_done INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()

    def _is_file_processed(self, drive_file_id: str) -> bool:
        """Check if a file has already been processed."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT 1 FROM gdrive_processed WHERE drive_file_id = ?",
            (drive_file_id,)
        )
        result = c.fetchone() is not None
        conn.close()
        return result

    def _mark_file_processed(self, drive_file_id: str, filename: str, job_id: str):
        """Mark a file as processed."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO gdrive_processed
               (drive_file_id, filename, job_id, processed_at, uploaded_to_done)
               VALUES (?, ?, ?, ?, 0)""",
            (drive_file_id, filename, job_id, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

    def _mark_uploaded_to_done(self, job_id: str):
        """Mark that results have been uploaded to Done folder."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "UPDATE gdrive_processed SET uploaded_to_done = 1 WHERE job_id = ?",
            (job_id,)
        )
        conn.commit()
        conn.close()

    def _get_job_drive_file_id(self, job_id: str) -> Optional[str]:
        """Get the Drive file ID associated with a job."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT drive_file_id FROM gdrive_processed WHERE job_id = ?",
            (job_id,)
        )
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def list_new_files(self) -> List[dict]:
        """
        List new PDF files in the upload folder that haven't been processed.

        Returns:
            List of file metadata dicts with id, name, mimeType
        """
        # Query for PDF files in the upload folder
        query = f"'{self.upload_folder_id}' in parents and trashed = false"

        results = self.service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name, mimeType, size, createdTime)',
            orderBy='createdTime'
        ).execute()

        files = results.get('files', [])

        # Filter to supported files that haven't been processed
        new_files = []
        for f in files:
            # Only process PDFs for now
            if f.get('mimeType') not in PDF_MIME_TYPES:
                continue
            if not self._is_file_processed(f['id']):
                new_files.append(f)

        return new_files

    def download_file(self, file_id: str, filename: str) -> Path:
        """
        Download a file from Google Drive.

        Args:
            file_id: Google Drive file ID
            filename: Original filename

        Returns:
            Path to downloaded file
        """
        request = self.service.files().get_media(fileId=file_id)

        # Generate unique local filename
        local_path = self.local_upload_dir / f"gdrive_{file_id}_{filename}"

        fh = BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()

        # Write to local file
        with open(local_path, 'wb') as f:
            f.write(fh.getvalue())

        return local_path

    def upload_results(self, job_id: str, original_filename: str) -> List[str]:
        """
        Upload job results to the Done folder in Google Drive.

        Args:
            job_id: The job ID
            original_filename: Original filename (without extension)

        Returns:
            List of uploaded file IDs
        """
        job_output_dir = self.local_output_dir / job_id
        if not job_output_dir.exists():
            return []

        uploaded_ids = []
        base_name = Path(original_filename).stem

        # Create a subfolder in Done for this job's outputs
        folder_metadata = {
            'name': f"{base_name}_output",
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [self.done_folder_id]
        }
        folder = self.service.files().create(
            body=folder_metadata,
            fields='id'
        ).execute()
        output_folder_id = folder.get('id')

        # Upload all files in the job output directory
        for file_path in job_output_dir.rglob('*'):
            if file_path.is_file():
                # Determine MIME type
                mime_type = 'application/octet-stream'
                suffix = file_path.suffix.lower()
                if suffix == '.md':
                    mime_type = 'text/markdown'
                elif suffix == '.json':
                    mime_type = 'application/json'
                elif suffix in ('.png', '.jpg', '.jpeg'):
                    mime_type = f'image/{suffix[1:]}'

                # Create relative path for nested files (like images/)
                rel_path = file_path.relative_to(job_output_dir)

                file_metadata = {
                    'name': file_path.name,
                    'parents': [output_folder_id]
                }

                media = MediaFileUpload(
                    str(file_path),
                    mimetype=mime_type,
                    resumable=True
                )

                uploaded = self.service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id'
                ).execute()

                uploaded_ids.append(uploaded.get('id'))

        # Mark as uploaded
        self._mark_uploaded_to_done(job_id)

        return uploaded_ids

    def move_to_done(self, file_id: str):
        """
        Move a file from Upload folder to Done folder in Drive.

        Args:
            file_id: Google Drive file ID
        """
        # Get current parent
        file = self.service.files().get(
            fileId=file_id,
            fields='parents'
        ).execute()
        previous_parents = ",".join(file.get('parents', []))

        # Move to Done folder
        self.service.files().update(
            fileId=file_id,
            addParents=self.done_folder_id,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()

    def poll_and_queue(self, add_to_queue_callback: Callable) -> List[str]:
        """
        Poll for new files and add them to the processing queue.

        Args:
            add_to_queue_callback: Function to call to add a file to the queue.
                Should accept (filepath, filename, file_size) and return job_id.

        Returns:
            List of job IDs that were queued
        """
        new_files = self.list_new_files()
        job_ids = []

        for file_info in new_files:
            file_id = file_info['id']
            filename = file_info['name']
            file_size = int(file_info.get('size', 0))

            print(f"[GDrive] Downloading: {filename}")

            try:
                # Download the file
                local_path = self.download_file(file_id, filename)

                # Add to queue
                job_id = add_to_queue_callback(
                    filepath=str(local_path),
                    filename=filename,
                    file_size=file_size
                )

                # Mark as processed
                self._mark_file_processed(file_id, filename, job_id)

                print(f"[GDrive] Queued: {filename} -> Job {job_id}")
                job_ids.append(job_id)

            except Exception as e:
                print(f"[GDrive] Error processing {filename}: {e}")

        return job_ids

    def on_job_complete(self, job_id: str, filename: str, success: bool):
        """
        Called when a job completes. Uploads results and moves source file.

        Args:
            job_id: The completed job ID
            filename: Original filename
            success: Whether the job completed successfully
        """
        drive_file_id = self._get_job_drive_file_id(job_id)
        if not drive_file_id:
            # Job wasn't from Google Drive
            return

        print(f"[GDrive] Job complete: {job_id} ({filename})")

        try:
            if success:
                # Upload results to Done folder
                uploaded = self.upload_results(job_id, filename)
                print(f"[GDrive] Uploaded {len(uploaded)} result files")

            # Move original file from Upload to Done
            self.move_to_done(drive_file_id)
            print(f"[GDrive] Moved source file to Done folder")

        except Exception as e:
            print(f"[GDrive] Error handling completion for {job_id}: {e}")


# Singleton instance
_poller_instance: Optional[GoogleDrivePoller] = None


def get_poller() -> Optional[GoogleDrivePoller]:
    """Get the Google Drive poller instance (if configured)."""
    global _poller_instance
    return _poller_instance


def init_poller(
    db_path: Path,
    upload_dir: Path,
    output_dir: Path
) -> Optional[GoogleDrivePoller]:
    """
    Initialize the Google Drive poller from environment variables.

    Returns None if not configured or if Google API is not available.
    """
    global _poller_instance

    # Check if enabled
    if os.environ.get('GDRIVE_ENABLED', '').lower() != 'true':
        print("[GDrive] Integration disabled (set GDRIVE_ENABLED=true to enable)")
        return None

    if not GOOGLE_API_AVAILABLE:
        print("[GDrive] Google API libraries not installed")
        print("[GDrive] Run: pip install google-api-python-client google-auth")
        return None

    # Get required environment variables
    credentials_path = os.environ.get('GDRIVE_CREDENTIALS_PATH')
    upload_folder_id = os.environ.get('GDRIVE_UPLOAD_FOLDER_ID')
    done_folder_id = os.environ.get('GDRIVE_DONE_FOLDER_ID')

    if not all([credentials_path, upload_folder_id, done_folder_id]):
        print("[GDrive] Missing required environment variables:")
        if not credentials_path:
            print("  - GDRIVE_CREDENTIALS_PATH")
        if not upload_folder_id:
            print("  - GDRIVE_UPLOAD_FOLDER_ID")
        if not done_folder_id:
            print("  - GDRIVE_DONE_FOLDER_ID")
        return None

    if not os.path.exists(credentials_path):
        print(f"[GDrive] Credentials file not found: {credentials_path}")
        return None

    try:
        _poller_instance = GoogleDrivePoller(
            credentials_path=credentials_path,
            upload_folder_id=upload_folder_id,
            done_folder_id=done_folder_id,
            db_path=db_path,
            local_upload_dir=upload_dir,
            local_output_dir=output_dir,
        )
        print("[GDrive] Integration initialized successfully!")
        print(f"[GDrive] Upload folder: {upload_folder_id}")
        print(f"[GDrive] Done folder: {done_folder_id}")
        return _poller_instance
    except Exception as e:
        print(f"[GDrive] Failed to initialize: {e}")
        return None

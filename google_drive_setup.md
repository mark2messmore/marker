# Google Drive Integration Setup

This guide explains how to set up the Google Drive integration for Marker, allowing you to:
- Drop PDF files into a Google Drive folder from any device
- Have them automatically processed by Marker on your server
- Get the results uploaded back to Google Drive

## Architecture

```
Work PC → Google Drive "MarkerUpload" folder
              ↓ (server polls every 60 sec)
         vast.ai server downloads & processes
              ↓
         Google Drive "MarkerDone" folder → Work PC downloads results
```

## Step 1: Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (e.g., "marker-gdrive")
3. Enable the **Google Drive API**:
   - Go to "APIs & Services" → "Library"
   - Search for "Google Drive API"
   - Click "Enable"

## Step 2: Create Service Account

1. Go to "APIs & Services" → "Credentials"
2. Click "Create Credentials" → "Service Account"
3. Fill in details:
   - Name: `marker-server`
   - Description: "Service account for Marker PDF converter"
4. Click "Create and Continue"
5. Skip the optional steps, click "Done"
6. Click on the created service account
7. Go to "Keys" tab → "Add Key" → "Create new key"
8. Choose **JSON** format
9. Download the JSON file (e.g., `marker-gdrive-credentials.json`)

**Important:** Note the service account email (looks like `marker-server@project-id.iam.gserviceaccount.com`)

## Step 3: Set Up Google Drive Folders

1. Open [Google Drive](https://drive.google.com/)
2. Create two folders:
   - `MarkerUpload` - where you'll drop files
   - `MarkerDone` - where results will appear

3. **Share both folders with the service account:**
   - Right-click folder → "Share"
   - Paste the service account email
   - Set permission to **Editor**
   - Click "Share" (ignore the "couldn't send email" warning)

4. **Get the folder IDs:**
   - Open each folder in Google Drive
   - The URL will be: `https://drive.google.com/drive/folders/FOLDER_ID_HERE`
   - Copy each folder ID

## Step 4: Install Dependencies

On your vast.ai server:

```bash
pip install google-api-python-client google-auth
```

## Step 5: Configure Environment Variables

Copy the credentials JSON file to your server, then set these environment variables:

```bash
# Enable the integration
export GDRIVE_ENABLED=true

# Path to your service account credentials JSON file
export GDRIVE_CREDENTIALS_PATH=/path/to/marker-gdrive-credentials.json

# Folder IDs from Step 3
export GDRIVE_UPLOAD_FOLDER_ID=your_upload_folder_id_here
export GDRIVE_DONE_FOLDER_ID=your_done_folder_id_here

# Optional: polling interval in seconds (default: 60)
export GDRIVE_POLL_INTERVAL=60
```

Or create a `.env` file:

```env
GDRIVE_ENABLED=true
GDRIVE_CREDENTIALS_PATH=/path/to/marker-gdrive-credentials.json
GDRIVE_UPLOAD_FOLDER_ID=your_upload_folder_id_here
GDRIVE_DONE_FOLDER_ID=your_done_folder_id_here
GDRIVE_POLL_INTERVAL=60
```

## Step 6: Start the Server

```bash
marker_ui --port 8000
```

You should see:

```
[GDrive] Integration initialized successfully!
[GDrive] Upload folder: your_upload_folder_id
[GDrive] Done folder: your_done_folder_id
[GDrive] Starting polling task (interval: 60s)
```

## Usage

### Automatic Processing

1. Drop a PDF into your `MarkerUpload` folder on Google Drive
2. Within 60 seconds (or your configured interval), the server will:
   - Download the file
   - Add it to the processing queue
   - Process it
   - Upload results to `MarkerDone` folder
   - Move the original PDF to `MarkerDone`

### Manual Sync

If you don't want to wait for the next poll:

```bash
# Check status
curl http://localhost:8000/api/gdrive/status

# See pending files
curl http://localhost:8000/api/gdrive/pending

# Trigger immediate sync
curl -X POST http://localhost:8000/api/gdrive/sync
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/gdrive/status` | GET | Check if Google Drive is enabled and configured |
| `/api/gdrive/pending` | GET | List files waiting in the upload folder |
| `/api/gdrive/sync` | POST | Manually trigger a sync (download & queue new files) |

## Troubleshooting

### "Google API libraries not installed"

Run: `pip install google-api-python-client google-auth`

### "Credentials file not found"

Make sure `GDRIVE_CREDENTIALS_PATH` points to the correct location of your JSON file.

### "Missing required environment variables"

All four variables are required:
- `GDRIVE_ENABLED=true`
- `GDRIVE_CREDENTIALS_PATH`
- `GDRIVE_UPLOAD_FOLDER_ID`
- `GDRIVE_DONE_FOLDER_ID`

### Files not being picked up

1. Make sure the service account has Editor access to both folders
2. Check that files are PDFs (currently only PDFs are supported)
3. Check the server logs for errors

### "Permission denied" or "File not found"

The service account doesn't have access to the folder. Re-share the folder with the service account email address.

## Security Notes

- The service account credentials JSON file contains sensitive data - keep it secure
- Only the service account can access the files (not your personal Google account)
- Consider using a dedicated Google account for this integration
- The credentials only have access to files explicitly shared with the service account

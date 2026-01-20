#!/bin/bash
# Vast.ai Setup Script for Marker PDF
# Run this after cloning the repo on a vast.ai GPU instance

set -e

echo "=== Marker PDF - Vast.ai GPU Setup ==="

# Check for NVIDIA GPU
if ! command -v nvidia-smi &> /dev/null; then
    echo "ERROR: nvidia-smi not found. Make sure you're on a GPU instance."
    exit 1
fi

echo "GPU detected:"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# Install Python dependencies
echo ""
echo "=== Installing Python dependencies ==="

# Install PyTorch with CUDA support first
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# Install marker
pip install -e .

# Install dev dependencies for full functionality
pip install pytest streamlit

# Verify CUDA is working
echo ""
echo "=== Verifying CUDA Setup ==="
python -c "
import torch
print(f'PyTorch version: {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'CUDA version: {torch.version.cuda}')
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'GPU Memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
else:
    print('WARNING: CUDA not available!')
"

# Verify marker imports
echo ""
echo "=== Verifying Marker Installation ==="
python -c "
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.settings import settings
print(f'Marker installed successfully')
print(f'TORCH_DEVICE_MODEL: {settings.TORCH_DEVICE_MODEL}')
print(f'MODEL_DTYPE: {settings.MODEL_DTYPE}')
"

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Usage examples:"
echo "  marker_single /path/to/file.pdf --output_dir ./output"
echo "  marker /path/to/folder --workers 2"
echo ""
echo "For best performance on large documents:"
echo "  marker_single file.pdf --output_dir ./output"
echo ""
echo "With LLM enhancement (requires GOOGLE_API_KEY in local.env):"
echo "  marker_single file.pdf --use_llm --output_dir ./output"

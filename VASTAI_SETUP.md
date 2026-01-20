# Marker PDF - Vast.ai GPU Setup Guide

This guide helps you run Marker on vast.ai for fast PDF-to-markdown conversion.

## Recommended Instance

- **GPU**: RTX 3090, RTX 4090, A100, or H100 (8GB+ VRAM minimum)
- **Image**: `pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime` or any PyTorch CUDA image
- **Disk**: 20GB+ (models are ~5GB)

## Quick Start

### Option 1: Direct Setup (Recommended)

1. **Create a vast.ai instance** with a PyTorch CUDA image

2. **Clone and setup**:
```bash
git clone https://github.com/datalab-to/marker.git
cd marker
chmod +x setup_vastai.sh
./setup_vastai.sh
```

3. **Convert your PDF**:
```bash
# Upload your PDF to the instance first
marker_single /path/to/your.pdf --output_dir ./output
```

### Option 2: Manual Setup

```bash
# Install PyTorch with CUDA
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# Clone and install marker
git clone https://github.com/datalab-to/marker.git
cd marker
pip install -e .

# Verify CUDA
python -c "import torch; print(f'CUDA: {torch.cuda.is_available()}')"

# Convert
marker_single your_file.pdf --output_dir ./output
```

## Performance Tips

### For Large Documents (50+ pages)

```bash
# Standard conversion
marker_single large_doc.pdf --output_dir ./output

# With LLM enhancement (better tables/math, requires API key)
export GOOGLE_API_KEY=your_key
marker_single large_doc.pdf --use_llm --output_dir ./output
```

### For Multiple Files

```bash
# Batch processing (faster than one-by-one)
marker /path/to/pdf_folder --output_dir ./output --workers 2
```

### For Maximum Speed

```bash
# If you have 24GB+ VRAM (A100, H100)
marker_single file.pdf --output_dir ./output

# For multi-GPU setups
NUM_DEVICES=2 NUM_WORKERS=4 marker_chunk_convert ./input_folder ./output_folder
```

## Expected Performance

| GPU | Pages/Second | 60-page PDF |
|-----|--------------|-------------|
| CPU only | ~0.02 | ~50 min |
| RTX 3090 | ~2-5 | ~15-30 sec |
| A100 | ~10-15 | ~5-10 sec |
| H100 | ~20-25 | ~3-5 sec |

## Troubleshooting

### CUDA Not Detected
```bash
# Check GPU
nvidia-smi

# Reinstall PyTorch with CUDA
pip uninstall torch torchvision torchaudio -y
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### Out of Memory
- Use a GPU with more VRAM
- Process fewer pages at once with `--page_range "0-20"`
- Reduce batch size (if applicable)

### Models Not Downloading
```bash
# Models download automatically on first run
# If issues, check internet connectivity and HuggingFace access
export HF_HUB_ENABLE_HF_TRANSFER=1
```

## Output Formats

```bash
# Markdown (default)
marker_single file.pdf --output_dir ./output

# JSON (with bounding boxes)
marker_single file.pdf --output_format json --output_dir ./output

# HTML
marker_single file.pdf --output_format html --output_dir ./output
```

#!/bin/bash
# Full Vast.ai Setup Script for Marker PDF + Claude Code CLI
# Run this as root on a fresh vast.ai instance

set -e

echo "=== Full Vast.ai Setup: Marker + Claude Code ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit 1
fi

# Check for NVIDIA GPU
if ! command -v nvidia-smi &> /dev/null; then
    echo "ERROR: nvidia-smi not found. Make sure you're on a GPU instance."
    exit 1
fi

echo ""
echo "GPU detected:"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# Variables
USERNAME="markeruser"
REPO_URL="https://github.com/mark2messmore/marker.git"
WORKDIR="/home/$USERNAME/marker"

# Step 1: Create user
echo ""
echo "=== Step 1: Creating user '$USERNAME' ==="
if id "$USERNAME" &>/dev/null; then
    echo "User $USERNAME already exists, skipping..."
else
    useradd -m -s /bin/bash $USERNAME
    echo "User $USERNAME created"
fi

# Step 2: Clone repo as the new user
echo ""
echo "=== Step 2: Cloning marker repo ==="
su - $USERNAME -c "
    if [ -d ~/marker ]; then
        echo 'Repo already exists, pulling latest...'
        cd ~/marker && git pull
    else
        git clone $REPO_URL ~/marker
    fi
"

# Step 3: Install system dependencies (as root)
echo ""
echo "=== Step 3: Installing system dependencies ==="
apt-get update
apt-get install -y curl git python3-pip python3-venv libgl1-mesa-glx libglib2.0-0

# Step 4: Install Claude Code CLI for the user
echo ""
echo "=== Step 4: Installing Claude Code CLI ==="
su - $USERNAME -c '
    curl -fsSL https://claude.ai/install.sh | bash
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.bashrc
    export PATH="$HOME/.local/bin:$PATH"
    echo "Claude CLI installed at: ~/.local/bin/claude"
'

# Step 5: Install marker dependencies
echo ""
echo "=== Step 5: Installing marker and dependencies ==="
su - $USERNAME -c '
    cd ~/marker

    # Install PyTorch with CUDA
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

    # Install marker
    pip install -e .

    # Verify CUDA
    python -c "
import torch
print(f\"PyTorch version: {torch.__version__}\")
print(f\"CUDA available: {torch.cuda.is_available()}\")
if torch.cuda.is_available():
    print(f\"CUDA version: {torch.version.cuda}\")
    print(f\"GPU: {torch.cuda.get_device_name(0)}\")
"

    # Verify marker
    python -c "from marker.converters.pdf import PdfConverter; print(\"Marker installed successfully\")"
'

# Step 6: Create convenience script for running Claude
echo ""
echo "=== Step 6: Creating startup script ==="
cat > /home/$USERNAME/start_claude.sh << 'SCRIPT'
#!/bin/bash
export PATH="$HOME/.local/bin:$PATH"
cd ~/marker
echo ""
echo "=== Marker + Claude Code Ready ==="
echo ""
echo "Commands:"
echo "  marker_single file.pdf --output_dir ./output    # Convert PDF"
echo "  claude --dangerously-skip-permissions           # Start Claude CLI"
echo ""
claude --dangerously-skip-permissions
SCRIPT
chmod +x /home/$USERNAME/start_claude.sh
chown $USERNAME:$USERNAME /home/$USERNAME/start_claude.sh

echo ""
echo "=============================================="
echo "=== Setup Complete! ==="
echo "=============================================="
echo ""
echo "To start using marker + Claude:"
echo ""
echo "  su - $USERNAME"
echo "  ./start_claude.sh"
echo ""
echo "Or manually:"
echo ""
echo "  su - $USERNAME"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
echo "  cd ~/marker"
echo "  marker_single /path/to/file.pdf --output_dir ./output"
echo "  claude --dangerously-skip-permissions"
echo ""

#!/bin/bash
set -eo pipefail
source ~/miniconda3/etc/profile.d/conda.sh
cd ~/MASt3R-SLAM
if ! conda env list | grep -q "^mast3r-slam "; then
  conda create -y -n mast3r-slam python=3.11
fi
conda activate mast3r-slam
echo "=== CUDA 12.8 toolkit (in env) ==="
[ -x $CONDA_PREFIX/bin/nvcc ] || conda install -y -c nvidia/label/cuda-12.8.1 cuda-toolkit
export CUDA_HOME=$CONDA_PREFIX
export PATH=$CUDA_HOME/bin:$PATH
nvcc --version | tail -2
echo "=== torch cu128 ==="
python -c "import torch" 2>/dev/null || pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
python -c "import torch;print(torch.__version__,torch.version.cuda,torch.cuda.get_device_capability());assert torch.cuda.get_device_capability()==(12,0);print(torch.ones(2,device='cuda')*2)"
pip freeze | grep -E "^torch(vision)?==" > /tmp/mast3r_torch_pin.txt
export TORCH_CUDA_ARCH_LIST="12.0"
export CC=/usr/bin/gcc-13 CXX=/usr/bin/g++-13 CUDAHOSTCXX=/usr/bin/g++-13 NVCC_CCBIN=/usr/bin/g++-13
export MAX_JOBS=16
pip install ninja "cython<3" wheel "setuptools<80" -c /tmp/mast3r_torch_pin.txt
echo "=== mast3r ==="
python -c "import torch, curope, mast3r" 2>/dev/null || pip install --no-build-isolation -e thirdparty/mast3r -c /tmp/mast3r_torch_pin.txt
echo "=== in3d ==="
pip install --no-build-isolation -e thirdparty/in3d -c /tmp/mast3r_torch_pin.txt
echo "=== mast3r_slam ==="
pip install --no-build-isolation -e . -c /tmp/mast3r_torch_pin.txt
pip install "opencv-python<4.12" "numpy<2" -c /tmp/mast3r_torch_pin.txt
python -c "import torch;print('torch after builds:',torch.__version__,torch.cuda.get_device_capability())"
echo "=== checkpoints ==="
mkdir -p checkpoints
cd checkpoints
for u in \
 https://download.europe.naverlabs.com/ComputerVision/MASt3R/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth \
 https://download.europe.naverlabs.com/ComputerVision/MASt3R/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_trainingfree.pth \
 https://download.europe.naverlabs.com/ComputerVision/MASt3R/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_codebook.pkl; do
  [ -s "$(basename $u)" ] || wget -q "$u"
done
ls -la
echo "=== INSTALL DONE ==="

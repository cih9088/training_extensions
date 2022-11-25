#!/usr/bin/env bash

set -eu

git submodule update --init --recursive

python -m venv ./venv
source ./venv/bin/activate

PYTORCH_VERSION=1.8.2+cu111
TORCHVISION_VERSION=0.9.2+cu111
MMCV_VERSION=1.5.0

pip install pip==21.2.1 wheel
pip install --upgrade setuptools

pip install torch==$PYTORCH_VERSION torchvision==$TORCHVISION_VERSION \
    -f https://download.pytorch.org/whl/lts/1.8/torch_lts.html

pip install mmcv-full==$MMCV_VERSION
sed -i "s/force=False/force=True/g" $(python -c 'import site; print(site.getsitepackages()[0])')/mmcv/utils/registry.py

pip install --editable ./

pip install mmdet==2.25.1
pip install mmsegmentation==0.29.1
pip install mmcls==0.24.1
pip install torchreid@git+https://github.com/openvinotoolkit/deep-object-reid@otx
pip install mmdeploy@git+https://git@github.com/open-mmlab/mmdeploy@v0.10.0

pip install numpy==1.21.0
pip install --editable ./external/model-preparation-algorithm/submodule

pip uninstall -y mmpycocotools
pip install --no-binary=mmpycocotools mmpycocotools

# for test purpose
pip install pytest onnxoptimizer

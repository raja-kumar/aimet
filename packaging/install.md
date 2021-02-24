# AIMET Installation and Setup
This page provides instructions to install AIMET package on ***Ubuntu 18.04 LTS with Nvidia GPU*** (see [system requirements]( docker_install.md#requirements)). Please follow the instructions in the order provided, unless specified otherwise.

- [Installation](#installation)
    - [Install prerequisite packages](#install-prerequisite-packages)
    - [Install AIMET packages](#install-aimet-packages)
    - [Install common debian packages](#install-common-debian-packages)
    - [GPU packages](#install-GPU-packages)
    - [Post installation steps](#post-installation-steps)
- [Environment Setup](#environment-setup)

## Installation

> **_NOTE:_**  
 1. Please pre-pend the "apt-get install" and "pip3 install" commands with "sudo -H" as appropriate.
 2. These instructions assume that pip packages will be installed in the path: /usr/local/lib/python3.6/dist-packages. If that is not the case, please modify it accordingly.

### Install prerequisite packages
Install the basic pre-requisite packages as follows:
```bash
apt-get update
apt-get install python3.6 python3.6-dev python3-pip
pip3 install --upgrade pip
```

### Install AIMET packages
Go to https://github.com/quic/aimet/releases and identify the release tag of the package you want to install. Replace `<release_tag>` in the steps below with the appropriate tag and install the AIMET packages in the order specified below:

> _NOTE:_ Python dependencies would automatically get installed.

```bash
release_tag=<release_tag>
pip3 install https://github.com/quic/aimet/releases/download/${release_tag}/AimetCommon-${release_tag}-py3-none-any.whl -f https://download.pytorch.org/whl/torch_stable.html
pip3 install https://github.com/quic/aimet/releases/download/${release_tag}/AimetTorch-${release_tag}-py3-none-any.whl
pip3 install https://github.com/quic/aimet/releases/download/${release_tag}/AimetTensorflow-${release_tag}-py3-none-any.whl
pip3 install https://github.com/quic/aimet/releases/download/${release_tag}/Aimet-${release_tag}-py3-none-any.whl
```

### Install common debian packages
Install the common debian packages from the packages_common.txt file as follows:
```bash
cat /usr/local/lib/python3.6/dist-packages/aimet_common/bin/packages_common.txt | xargs apt-get --assume-yes install
```

#### Replace Pillow with Pillow-SIMD
*Optional*: Replace the Pillow package with Pillow-SIMD as follows:
```bash
pip3 uninstall -y pillow && pip3 install --no-cache-dir Pillow-SIMD==6.0.0.post0
```

### Install GPU packages
Prepare the environment for installation of GPU packages as follows:
```bash
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu1804/x86_64/cuda-repo-ubuntu1804_10.0.130-1_amd64.deb
apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/cuda/repos/ubuntu1804/x86_64/7fa2af80.pub
dpkg -i cuda-repo-ubuntu1804_10.0.130-1_amd64.deb
apt-get update
wget http://developer.download.nvidia.com/compute/machine-learning/repos/ubuntu1804/x86_64/nvidia-machine-learning-repo-ubuntu1804_1.0.0-1_amd64.deb
apt-get --assume-yes install ./nvidia-machine-learning-repo-ubuntu1804_1.0.0-1_amd64.deb
apt-get update
```

Now install the GPU packages from the packages_common.txt file:
```bash
cat /usr/local/lib/python3.6/dist-packages/aimet_common/bin/packages_gpu.txt | xargs apt-get --assume-yes install
```

### Post installation steps
Perform the following post-installation steps:
```bash
ln -s /usr/local/cuda-10.0 /usr/local/cuda
ln -s /usr/lib/x86_64-linux-gnu/libjpeg.so /usr/lib
```

## Environment setup
Set the common environment variables as follows:
```bash
source /usr/local/lib/python3.6/dist-packages/aimet_common/bin/envsetup.sh 
```

Add the AIMET package location to the environment paths as follows:
```bash
export LD_LIBRARY_PATH=/usr/local/lib/python3.6/dist-packages/aimet_common/x86_64-linux-gnu:/usr/local/lib/python3.6/dist-packages/aimet_common:$LD_LIBRARY_PATH

if [[ $PYTHONPATH = "" ]]; then export PYTHONPATH=/usr/local/lib/python3.6/dist-packages/aimet_common/x86_64-linux-gnu; else export PYTHONPATH=/usr/local/lib/python3.6/dist-packages/aimet_common/x86_64-linux-gnu:$PYTHONPATH; fi
```

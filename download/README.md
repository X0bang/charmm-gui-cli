# 下载 0.3.0

- [源码安装包（推荐）](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm-gui-cli-0.3.0.tar.gz)
- [Python wheel](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm_gui_cli-0.3.0-py3-none-any.whl)
- [SHA256SUMS](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/SHA256SUMS)

Ubuntu 22.04+，Python 3.10+ 和 venv。解压源码包后：

```bash
tar -xzf charmm-gui-cli-0.3.0.tar.gz
cd charmm-gui-cli-0.3.0
bash install.sh
```

首次安装需联网获取依赖，包括 RDKit。GROMACS 为可选项，不包含在安装包中。

已有独立 Python 环境的用户也可安装 wheel（包含化学依赖）：

```bash
python -m pip install './charmm_gui_cli-0.3.0-py3-none-any.whl[chem]'
```

将两个安装包和 SHA256SUMS 放在同一目录后，可执行 `sha256sum -c SHA256SUMS` 校验。

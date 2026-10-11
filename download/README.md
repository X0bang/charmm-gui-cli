# 下载 0.5.0

- [源码安装包（推荐）](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm-gui-cli-0.5.0.tar.gz)
- [Python wheel](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm_gui_cli-0.5.0-py3-none-any.whl)
- [SHA256SUMS](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/SHA256SUMS)

Ubuntu 22.04+，Python 3.10+ 和 venv。解压源码包后：

```bash
tar -xzf charmm-gui-cli-0.5.0.tar.gz
cd charmm-gui-cli-0.5.0
bash install.sh
```

首次安装需联网获取依赖，包括 RDKit。GROMACS 为可选项，不包含在安装包中。

已有独立 Python 环境的用户也可安装 wheel（包含化学依赖）：

```bash
python -m pip install './charmm_gui_cli-0.5.0-py3-none-any.whl[chem]'
```

0.5.0 增加直观进度、空间预检与 PNG、终端/离线 HTML 报告、参数扫描与前处理复用、实验性多组分，以及归档归属校验和中文恢复引导。真实扫描完整验收及多组分远端实测尚未完成，不在本地自动运行 MD。旧版安装包继续保留。

SHA256SUMS 包含新旧安装包；放在同一目录可执行 `sha256sum -c SHA256SUMS`，只下载某一版时可用 `sha256sum --ignore-missing -c SHA256SUMS`。

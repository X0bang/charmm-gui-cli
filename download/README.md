# 下载 0.5.1

- [源码安装包（推荐）](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm-gui-cli-0.5.1.tar.gz)
- [Python wheel](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm_gui_cli-0.5.1-py3-none-any.whl)
- [SHA256SUMS](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/SHA256SUMS)

Ubuntu 22.04+，Python 3.10+ 和 venv。解压源码包后：

```bash
tar -xzf charmm-gui-cli-0.5.1.tar.gz
cd charmm-gui-cli-0.5.1
bash install.sh
```

首次安装需联网获取依赖，包括 RDKit。GROMACS 为可选项，不包含在安装包中。

已有独立 Python 环境的用户也可安装 wheel（包含化学依赖）：

```bash
python -m pip install './charmm_gui_cli-0.5.1-py3-none-any.whl[chem]'
```

0.5.1 修复官网归档压缩等待、多组分 PDB 边界及有据可查的原子重命名，完成双浓度扫描和真实多组分建膜验收。包含进度引导、结构预检、PNG、离线 HTML、批量及参数扫描；多组分仍为实验性。不在本地自动运行 MD，旧版安装包继续保留。

SHA256SUMS 包含新旧安装包；放在同一目录可执行 `sha256sum -c SHA256SUMS`，只下载某一版时可用 `sha256sum --ignore-missing -c SHA256SUMS`。

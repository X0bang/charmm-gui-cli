# 下载

## 0.6.1 安装包

- [源码安装包](charmm-gui-cli-0.6.1.tar.gz)
- [Python wheel](charmm_gui_cli-0.6.1-py3-none-any.whl)
- [校验和](SHA256SUMS)

完成后自动交付到创建任务时所在目录的 `./任务名/`；支持 `jobs export NAME [--to DIR]` 导出旧任务，内部证据保留。成功下载的临时文件会清理。已有安装可用 `charmm-gui-cli update` 更新。

```bash
tar -xzf charmm-gui-cli-0.6.1.tar.gz
cd charmm-gui-cli-0.6.1
bash install.sh
```

## 0.6.0 安装包

- [源码安装包](charmm-gui-cli-0.6.0.tar.gz)
- [Python wheel](charmm_gui_cli-0.6.0-py3-none-any.whl)
- [校验和](SHA256SUMS)

本版改为默认后台运行和当前目录输出；脚本/Slurm 请加全局 `--foreground`。支持输入格式自动修复、任务日志跟踪与 GET-only 核对恢复。详见 [CHANGELOG](../CHANGELOG.md) 和 [README](../README.md)。GitHub Release 发布状态见 [Releases](https://github.com/X0bang/charmm-gui-cli/releases)。

```bash
tar -xzf charmm-gui-cli-0.6.0.tar.gz
cd charmm-gui-cli-0.6.0
bash install.sh
```

## 0.5.2

- [源码安装包（推荐）](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm-gui-cli-0.5.2.tar.gz)
- [Python wheel](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/charmm_gui_cli-0.5.2-py3-none-any.whl)
- [SHA256SUMS](https://github.com/X0bang/charmm-gui-cli/raw/refs/heads/main/download/SHA256SUMS)

Ubuntu 22.04+，Python 3.10+ 和 venv。解压源码包后：

```bash
tar -xzf charmm-gui-cli-0.5.2.tar.gz
cd charmm-gui-cli-0.5.2
bash install.sh
```

首次安装需联网获取依赖，包括 RDKit。GROMACS 为可选项，不包含在安装包中。

已有独立 Python 环境的用户也可安装 wheel（包含化学依赖）：

```bash
python -m pip install './charmm_gui_cli-0.5.2-py3-none-any.whl[chem]'
```

0.5.2 新增自动语言检测与手动中英文切换、人类可读终端摘要及 `--json`、登录凭据错误识别、`update --check` / `update` 校验更新。保留原有建模、恢复和验收功能，不在本地自动运行 MD，旧版安装包继续保留。

尚无 update 的旧版本先手动安装本版；安装后可执行 `charmm-gui-cli update --check`。临时中文用 `charmm-gui-cli --lang zh update`，长期设置可在 `~/.bashrc` 中加入 `export CHARMM_GUI_CLI_LANG=zh`。

SHA256SUMS 包含新旧安装包；放在同一目录可执行 `sha256sum -c SHA256SUMS`，只下载某一版时可用 `sha256sum --ignore-missing -c SHA256SUMS`。

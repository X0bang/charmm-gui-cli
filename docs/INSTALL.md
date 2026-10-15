# Ubuntu 22.04+ 与 macOS 用户级安装

安装器支持 Python 3.10+，适用于 Ubuntu 22.04+ 和 macOS 的常规 Python/venv 环境。它不调用 sudo/apt，不改系统 Python，不要求 CHARMM-GUI 密码。首次安装通过 pip 下载项目依赖，需要网络和足够磁盘空间；安装包不是离线依赖全集。macOS 如有多个 Python，可通过下方 `--python` 指定可创建 venv 的解释器；不依赖系统自带 Python。

## 一键安装

从源码目录或解压后的发布源包目录执行：

```bash
bash install.sh
```

默认安装到 `~/.local/share/charmm-gui-cli/releases/<版本-时间-随机标识>/venv`，在 `~/.local/bin` 创建项目声明的命令入口。每次运行创建新版本目录，旧版本和失败记录均保留。重新安装时只更新安装器识别并标记的入口链接，不覆盖未知文件。

若系统提示缺少 Python 或 venv，需用户/管理员先安装相应系统包，例如 Ubuntu 的 `python3`、[`python3-venv`](https://packages.ubuntu.com/jammy/python3-venv)；自定义 Python 可能需要匹配版本的 `python3.X-venv`。安装器会给出提示，自己不会安装系统包。RDKit 的 pip 安装方式见[官方安装文档](https://www.rdkit.org/docs/Install.html)。

```bash
# 可指定解释器（--python 必须放在最前面）和安装位置；全程不交互
bash install.sh --python /usr/bin/python3 \
  --prefix /path/to/user-software/charmm-gui-cli \
  --bin-dir /path/to/user-software/bin

# 查看全部选项
bash install.sh --help
```

也可通过 `CHARMM_GUI_PYTHON`、`CHARMM_GUI_INSTALL_ROOT`、`CHARMM_GUI_BIN_DIR` 设置相同配置。不要用 sudo 运行用户级安装器。若安装目录不在 PATH，可直接运行 `~/.local/bin/charmm-gui-cli`，或在自己的 shell 配置中添加 `~/.local/bin`。

```bash
~/.local/bin/charmm-gui-cli --version
~/.local/bin/charmm-gui-cli --help
```

开始建模的登录、独立蛋白 PDB/配体 SDF 配置、运行与验证见 [使用手册](USAGE.md)。批量清单见 [批量建模](BATCH.md)，命名任务与恢复见 [恢复说明](RECOVERY.md)。安装器包含化学依赖，但不安装 GROMACS；`--grompp` 需另外配置 GROMACS。它也不安装浏览器。

## 离线安装

在同 OS、CPU 架构和 Python 版本的联网环境准备依赖 wheel 目录（包括 RDKit 依赖），再复制到目标机器。使用：

```bash
bash install.sh --offline --wheelhouse /path/to/wheels
```

若依赖不完整，pip 会失败并保留本次安装目录；不会切换已正常工作的命令入口。默认使用 pip 的索引/代理配置；安装器不更改配置，也不把索引凭据写进发布包。

## 升级与旧版本

通过本项目安装器管理的安装支持：

```bash
charmm-gui-cli update --check
charmm-gui-cli update
charmm-gui-cli --lang zh update
charmm-gui-cli --json update --check
```

从本仓库 GitHub Releases 查询最新正式发布，读取对应标签的源码包和 SHA256SUMS，校验后调用用户级安装器。不会把尚未发布的 main 分支版本当作可更新版本，也不自动降级。校验和通过同一发布的 HTTPS 资产获取，不是独立数字签名；需信任仓库及其发布流程。仍查询旧 download 路径的更新器，需从 Releases 下载源码并执行一次 `bash install.sh`，或 `git pull` 后重新安装。

自动识别本安装器管理的安装根和命令目录；自定义目录仍可自动识别。其他安装方式不自动修改，可按原 pip/venv 方式升级，或明确创建隔离安装：

```bash
charmm-gui-cli update --prefix /path/to/user-software/charmm-gui-cli \
  --bin-dir /path/to/user-software/bin
```

两个路径选项必须同时提供；不会覆盖无法识别的现有命令。更新下载 GitHub 源包，并根据原 pip 配置安装依赖，需要网络、venv/ensurepip 和足够磁盘。更新锁防止同一安装根的并发更新。校验、依赖安装或健康检查失败不切换入口；中断时可再次运行更新。临时下载保留在安装根 `updates/`，旧版本/失败安装保留在 `releases/`。不读取或改写登录会话、任务和模型，也不会自动重提任何建模任务。

尚无 `update` 的旧版本先获取新版源码/源包，执行一次 `bash install.sh`。新版本源码/源包也仍可直接执行该安装命令。旧版本可通过其 `venv/bin/charmm-gui-cli` 直接运行。安装器不提供旧安装版本的自动删除或卸载。`jobs clean` 仅用于可恢复地隔离任务，不用于清理安装目录，也不释放磁盘空间。

## 构建发布包

```bash
python3 scripts/build_release.py --out /path/to/new-release-directory
```

输出标准 `py3-none-any.whl`、源码 `.tar.gz`、`SHA256SUMS`。构建器只用 Python 标准库，拒绝覆盖已有发布文件，保留中断产物，不使用会清理构建目录的临时构建流程。源包使用显式白名单，只含项目 Python 源码、指定文档/脚本/示例与测试代码；wheel 只含 Python 包与必需安装元数据。

不包含 token、Cookie、runs、.venv、build、上游克隆、用户 test-dataset 结构或任意递归目录内容。CrtW 示例需用户自行保留的测试结构；独立蛋白/SDF 使用者直接填写自己的路径。加入新的交付文档/示例时，需更新 `scripts/build_release.py` 的白名单。发布前应核对 `SHA256SUMS` 并检查文件清单。

## 平台验证范围

GitHub CI 在 Ubuntu 22.04 和 macOS、Python 3.10 和 3.13 的四种组合上运行中英文回归，并验证发布打包。最近正式发布对应提交的四组测试已通过；具体历史记录见 [CHANGELOG](../CHANGELOG.md)。Python 3.14、所有 CPU 架构及每种系统 Python 安装方式并未逐一验收。

公开源码包可以从新目录运行测试；私有结构不随包分发，依赖它们的测试会明确跳过。源码包的一键安装、跨目录版本/帮助/doctor，以及 wheel 的文件清单、RECORD 和 SHA256 都单独检查。详见 [测试与验收](TESTING.md)。

CI 和本地安装检查不访问 CHARMM-GUI 建模服务，不替代真实账户、系统凭据库、桌面通知、远端建模或科学模型验收。GROMACS 是可选的外部程序；即使 doctor 找到了它，也不表示运行过模型编译或模拟。

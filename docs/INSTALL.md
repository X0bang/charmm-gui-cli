# Ubuntu 22.04+ 用户级安装与发布

安装器支持 Python 3.10+，适用于 Ubuntu 22.04 及更新版本的常规 Python 环境。它不调用 sudo/apt，不改系统 Python，不要求 CHARMM-GUI 密码。首次安装通过 pip 下载 requests、PyYAML、BeautifulSoup、RDKit 及其依赖，需要网络和足够磁盘空间；安装包不是离线依赖全集。

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

0.5.2 起支持：

```bash
charmm-gui-cli update --check
charmm-gui-cli update
charmm-gui-cli --lang zh update
charmm-gui-cli --json update --check
```

从本仓库公开 `main` 读取版本信息，将源码包与 SHA256SUMS 固定到同一 Git 提交，校验后调用用户级安装器。只更新版本更高且已发布校验和的源包，不安装开发分支或自动降级。校验和通过相同仓库的 HTTPS 获取，不是独立数字签名；需信任仓库及其发布流程。

自动识别本安装器管理的安装根和命令目录；自定义目录仍可自动识别。其他安装方式不自动修改，可按原 pip/venv 方式升级，或明确创建隔离安装：

```bash
charmm-gui-cli update --prefix /path/to/user-software/charmm-gui-cli \
  --bin-dir /path/to/user-software/bin
```

两个路径选项必须同时提供；不会覆盖无法识别的现有命令。更新下载 GitHub 源包，并根据原 pip 配置安装依赖，需要网络、venv/ensurepip 和足够磁盘。更新锁防止同一安装根的并发更新。校验、依赖安装或健康检查失败不切换入口；中断时可再次运行更新。临时下载保留在安装根 `updates/`，旧版本/失败安装保留在 `releases/`。不读取或改写登录会话、任务和模型，也不会自动重提任何建模任务。

尚无 `update` 的旧版本先获取新版源码/源包，执行一次 `bash install.sh`。新版本源码/源包也仍可直接执行该安装命令。旧版本可通过其 `venv/bin/charmm-gui-cli` 直接运行。没有卸载或自动清理命令；如何处理旧目录由用户决定。

## 构建发布包

```bash
python3 scripts/build_release.py --out /path/to/new-release-directory
```

输出标准 `py3-none-any.whl`、源码 `.tar.gz`、`SHA256SUMS`。构建器只用 Python 标准库，拒绝覆盖已有发布文件，保留中断产物，不使用会清理构建目录的临时构建流程。源包使用显式白名单，只含项目 Python 源码、指定文档/脚本/示例与测试代码；wheel 只含 Python 包与必需安装元数据。

不包含 token、Cookie、runs、.venv、build、上游克隆、用户 test-dataset 结构或任意递归目录内容。CrtW 示例需用户自行保留的测试结构；独立蛋白/SDF 使用者直接填写自己的路径。加入新的交付文档/示例时，需更新 `scripts/build_release.py` 的白名单。发布前应核对 `SHA256SUMS` 并检查文件清单。

## 平台验证说明

2026-10-11 的 0.5.1 发行代码已在宿主全新隔离依赖环境运行 546 项全部通过；Ubuntu 22.04 / Python 3.10.12 从公开源包以非 root 用户全新安装，运行 546 项，521 通过、25 因未分发私有结构等条件明确跳过。跨目录帮助、版本及 doctor 通过。源码白名单 88 文件、wheel 31 模块、RECORD 哈希和 GPL 元数据核对完成。真实双浓度扫描、多组分最终验收及 GROMACS 编译在宿主完成；容器无账户和 GROMACS，不把安装检查当作远端建模实测。

2026-10-11 的 0.5.1 压缩恢复与 TER 边界修复候选，已在 Ubuntu 22.04 / Python 3.10.12 中从源码以非 root 用户全新隔离安装，doctor 通过；公开候选源包及新安装依赖环境各运行 529 项测试，504 通过、25 因未分发私有结构等条件明确跳过。宿主 Python 3.13 的新隔离安装环境运行 529 项全部通过。旧安装和失败源任务均保留。官网网络恢复后，原真实双浓度扫描也已通过最终模型及 GROMACS 验收；这与无凭据容器的安装测试是不同证据。

2026-10-11 的 0.5.0 扩展版已重新在新建 Ubuntu 22.04 / Python 3.10.12 容器中以非 root 用户一键安装，RDKit 2026.9.1、NumPy 2.2.6、Pillow 12.3.0 及基础依赖成功安装；doctor 和报告帮助通过。公开源包全套 512 项测试，487 通过、25 明确跳过，退出码 0。宿主 Ubuntu 22.04.5 的 Python 3.13.13 新隔离安装也成功，512 项全部通过。用于下载依赖的可达镜像并不证明 CHARMM-GUI 连接可用；本轮官网/API 的 TLS 连接失败、真实扫描和多配体验收尚未完成。

2026-10-02 已在本地 `nvidia/cuda:12.0.0-base-ubuntu22.04` 容器中验证：容器报告 Ubuntu 22.04.3 LTS；先验证缺少 Python 的明确提示，再仅在容器内安装 Python 3.10.12/venv，然后以非 root 用户运行发布源包中的 `bash install.sh`。安装到该用户的默认 `~/.local`，RDKit 2026.3.6 和其他依赖安装成功；`--version`、`--help`、`doctor` 通过。再次安装也通过，命令链接切换到新目录，原版本目录完整保留。此容器没有 GROMACS，doctor 正确将其报告为可选项。

同一 Ubuntu 22.04 / Python 3.10 容器对不含用户测试结构的 0.4.0 代码源码包执行 `python -m unittest discover -s tests -q`：325 项测试，300 项通过、25 项按缺少私有结构等条件跳过，退出码 0。宿主完整 325 项全部通过，其中包含发布/安装器测试（wheel RECORD 哈希、源码白名单、pip 离线装本地 wheel、拒绝未知入口覆盖等）。

0.4.0 以非 root 用户重新运行一键安装，`doctor`、全局帮助和批量帮助通过；未配置登录凭据时，从其他工作目录执行两项合成蛋白/SDF 的 `batch --dry-run`，结果均为 `inputs_ready`，生成 JSON/CSV 汇总。旧 0.3.0 安装保留。宿主已升级到 0.4.0，并从其他目录无 token 恢复两个真实归档的批次，返回两项 `validated` 和 GROMACS 编译通过。

0.5.0 在同一 Ubuntu 22.04 / Python 3.10 容器完成公开源码包全套 485 项（460 通过、25 明确跳过）及非 root 一键升级。独立 `check`/PNG 与 `sweep --dry-run` 合成输入用例无需认证；doctor 验证 NumPy/Pillow/RDKit，扫描帮助通过。宿主全套 485 项通过，并检查源码白名单、wheel RECORD/许可证和安装后的 30 个模块内容一致。此前安装、候选包和失败产物不删除。

这些结果验证 Ubuntu 22.04 用户级安装、CLI/依赖检查和离线自动化测试，不是容器内远端建模或 GROMACS 验收；科学建模结果见项目验收记录。发布后的新增改动应重新执行相同检查。

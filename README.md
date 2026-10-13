# charmm-gui-cli

通过命令行构建膜蛋白–配体脂膜体系，无需浏览器或手工管理中间文件。独立实验性客户端，非 CHARMM-GUI 官方软件。

## 安装

支持 Ubuntu 22.04+ 和 macOS、Python 3.10+，需要 venv。安装器自动安装包括 RDKit 在内的依赖，不修改系统 Python。

```bash
git clone https://github.com/X0bang/charmm-gui-cli.git
cd charmm-gui-cli
bash install.sh
```

也可从 [GitHub Releases](https://github.com/X0bang/charmm-gui-cli/releases) 下载源码安装包，解压后运行 `bash install.sh`。若找不到命令，执行 `export PATH="$HOME/.local/bin:$PATH"`。

## 更新

```bash
charmm-gui-cli update --check  # 只检查已发布版本
charmm-gui-cli update          # 校验下载包并更新，不删除旧安装
```

更新仅使用 GitHub Releases 的最新正式安装包，校验 SHA256；失败不切换命令入口，不修改登录会话或建模数据。旧版更新器仍使用仓库 download 路径时，请从 Releases 下载新版源码，运行一次 `bash install.sh`，或在源码目录运行 `git pull` 后重新安装。自动更新适用于本项目安装器创建的隔离安装；pip 安装请使用原安装方式升级。

## 使用

```bash
charmm-gui-cli doctor
charmm-gui-cli login

# 蛋白与配体必须处于同一结合姿势坐标系
charmm-gui-cli build \
  --name receptor-a \
  --protein protein.pdb --ligand bound-ligand.sdf \
  --upper POPC=1 --lower POPC=1 \
  --salt KCl --salt-concentration 0.10
```

已有复合物 PDB，且包含可信 CONECT 键级时：

```bash
charmm-gui-cli build --complex complex.pdb \
  --ligand-resname LIG --accept-conect-bond-orders
```

自动完成输入准备、上传、参数化、建膜、下载和验证；登录会话及中间文件由程序管理。

脂质比例可写为 `--upper POPC:POPE=7:3`。名称先通过本地常用脂质目录检查；目录不含所有官网脂质，特殊名称核对后可使用 `--allow-unknown-lipids`（YAML：`membrane.allow_unknown_lipids: true`）。常用名称参考[官网脂质库](https://www.charmm-gui.org/?doc=archive&lib=lipid)，不代表每个 API 模块都支持全部名称。

末端补丁支持按链设置，例如 `--n-terminal A=GLYP,B=NTER`，也可用 `PROA` 等 segment 名；YAML 使用 `preparation.terminals: {A: {n_terminal: GLYP}, B: {n_terminal: NTER}}`。未指定的链使用统一默认值。

`check` 显示失败原因、自动修复和结构审阅项，`check --full` 显示完整证据；`--json`、`--lang`、`--foreground` 可放在子命令前后。新任务本地准备失败不会占住任务名，修正输入后可以重试。

### 后台运行与恢复

`build`、`build-resume` 和 `jobs resume` 默认在后台运行。启动前在前台完成本地准备和登录检查；启动后显示任务名、PID、日志和查看命令。关闭终端不影响任务，退出 `watch` 也不会停止建模。

```bash
charmm-gui-cli jobs list
charmm-gui-cli jobs watch receptor-a   # Ctrl-C 仅退出查看
charmm-gui-cli jobs status receptor-a
charmm-gui-cli --foreground build system.yaml --name receptor-b
charmm-gui-cli --foreground jobs resume receptor-b  # 脚本/Slurm：等待并返回最终退出码
```

网页建模 POST 默认读取超时 900 秒，等待期间每 30 秒记录心跳；可用 `--post-timeout 1800` 调整。只读 GET 保持原超时，结果不明的 POST 不自动重发。

```bash
charmm-gui-cli jobs reconcile receptor-a  # 仅 GET 核对提交结果
# 仅在人工确认官网未接收提交后使用：
charmm-gui-cli jobs reconcile receptor-a --confirm-not-submitted
```

核对成功后再 `jobs resume`。上传响应完全丢失源任务 ID 时，先在官网确认，再加 `--source-jobid ID`。无法判断时保留意图并显示官网链接；旧意图归档保留，不删除。

预检位于 `charmm-gui-checks/<时间戳>/`。`check --open` 可打开 PNG 预览。

### 一个任务，一个目录

新任务在运行命令的当前目录创建 `./<任务名>/`，`--out DIR` 可指定完整任务目录。压缩包和解压模型各只存一份，直接下载和验收到这个目录，不另建 `charmm-gui-runs/<任务名>/`，不在完成时复制一遍。

```text
receptor-a/
  charmm-gui.tgz       官网原始压缩包
  model/              解压模型和模拟输入
  report.txt          验收结论
  inputs/             实际上传的结构；system-config.json 区分 requested/applied
  run.log             完整日志，可以 tail -f
  README.txt
  .charmm-gui/        状态、提交意图、快照、下载回执和哈希证据
```

完成摘要显示任务目录。请保留整个目录，删除唯一的归档或内部证据会使验收报告无法复核。全局任务索引单独保存在 `~/.local/share/charmm-gui-cli/index/`（遵循 `XDG_DATA_HOME`）；登录会话仍在 `~/.config/charmm-gui-cli/`。索引不包含账户或结构副本，`jobs` 可在任意目录查询。旧任务仍按原布局读取，不自动搬迁。

```bash
charmm-gui-cli jobs export receptor-a
charmm-gui-cli jobs export receptor-a --to ./my-model
```

新任务 `jobs export NAME` 只更新人类可读报告，不复制大文件；显式 `--to` 才创建独立结果副本。改过的 `report.txt` 和 `README.txt` 会先保留为 `.user-edited` 文件，再生成新报告。模型和输入配置的改动不会被覆盖。旧任务 `export` 保留原有独立导出行为。`--to` 指定目录本身，不是父目录；其他任务的目录或用户改动过的独立导出文件不会被覆盖。

旧任务导出保留报告中的原验收版本，仍核对归档、输入、参数和编译产物，不视为新版重新验收。

验收未通过的任务也可交付诊断报告，报告和终端仍明确显示失败，不会标成通过。尚未完成下载及验收的任务不能导出。成功下载后清理本次 `.download-*` 与 `.normalized-*`；导出及下载恢复时，也会清理与已验证归档字节完全匹配的历史临时副本。失败或不匹配的响应保留用于诊断，不做全目录清理。

批次和扫描是多任务容器，默认放入当前目录 `charmm-gui-runs/`，每个子任务内使用同样的单目录布局；独立 `preview` 输出在 `charmm-gui-previews/`。批次调度保持前台运行方式。

输入准备自动修复可安全重编号的蛋白原子序号、可推断的元素列、空链 ID；记录写入 `check.json` 和准备报告，不修改原始输入。涉及歧义 CONECT 的重复序号仍报错。Gly/Pro 末端支持 `GLYP`/`PROP`，默认 `NTER` 不可用时依据官网选项调整并记录。

也可复制 [默认配置模板](examples/default-build.yaml)，修改输入路径、脂质比例和其他参数：

```bash
cp examples/default-build.yaml system.yaml
# 编辑 system.yaml 后运行
charmm-gui-cli build system.yaml --name receptor-a
```

配置文件必须显式传入；不与 `--protein`、`--upper` 等结构/模型选项混用。模板下载、完整参数示例、本地检查与恢复步骤见 [配置文件建模教程](docs/ADVANCED.md#用-yaml-配置文件建模)。

```bash
charmm-gui-cli -h            # 快速引导
charmm-gui-cli build -h      # 输入组合和建模参数
charmm-gui-cli jobs          # 任务及结果位置
charmm-gui-cli jobs status receptor-a
charmm-gui-cli jobs resume receptor-a
charmm-gui-cli jobs report receptor-a
charmm-gui-cli jobs report receptor-a --format text
charmm-gui-cli jobs report receptor-a --format html --out result.html
charmm-gui-cli build-resume  # 继续最近任务
```

主要输出为任务目录内的 `charmm-gui.tgz`、`model/` 和 `report.txt`；完整 JSON 验收证据在 `.charmm-gui/results/validation.json`。检测到 GROMACS 时自动执行输入编译检查，不运行 MD。

批量建模使用 [清单示例](examples/batch.yaml)：

```bash
charmm-gui-cli batch batch.yaml --out runs/campaign --dry-run
charmm-gui-cli batch-resume runs/campaign --max-active 2
charmm-gui-cli batch-status runs/campaign
```

每项自动验收，批次自动生成 JSON/CSV 汇总。

新增本地预检、三视图 PNG 和参数扫描：

```bash
charmm-gui-cli check --protein protein.pdb --ligand bound-ligand.sdf
charmm-gui-cli preview receptor-a
charmm-gui-cli sweep sweep.yaml --dry-run
charmm-gui-cli sweep-resume SCAN_DIR --max-active 2
charmm-gui-cli build --complex bound.pdb --components components.yaml
```

扫描使用 [清单示例](examples/sweep.yaml)，复用核验过的前处理；[多组分清单](examples/components.yaml) 为实验性入口。不在本地自动运行最小化、平衡或生产模拟。

## 语言与输出

支持中文与英文，默认英文。自动检测优先级为 `--lang` > `CHARMM_GUI_CLI_LANG` > `LANGUAGE`（按冒号分隔顺序）> `LC_ALL` > `LC_MESSAGES` > `LANG`。中文区域映射为中文，其他语言映射为英文；空值、`C`、`POSIX`（包括 `C.UTF-8`）跳过并继续检测。

```bash
charmm-gui-cli --lang zh login
charmm-gui-cli --lang en build -h
charmm-gui-cli --json jobs status receptor-a

# 手动固定当前终端语言，不改系统 locale
export CHARMM_GUI_CLI_LANG=zh  # 中文；改为 en 切换英文
# unset CHARMM_GUI_CLI_LANG   # 恢复按系统语言自动检测
```

全局参数可放在子命令前或后。login、check、build/build-resume、status、update 和 jobs list/status/resume 在交互终端显示易读摘要；重定向、管道、未注册摘要的命令及 `--json` 保持 JSON 输出。JSON 字段名、结构及错误 category 不翻译。`jobs report --format text/html` 可显式选择报告格式。服务器语言为 C 或英文时，可在 `~/.bashrc` 设置 `export CHARMM_GUI_CLI_LANG=zh`，再打开新终端。尚未迁移的旧英文文案见 [清单](docs/I18N-TODO.md)。

## 科学边界

本工具不做分子对接、不自动选择质子化状态或修补缺失片段；配体需要可靠的化学定义。格式修复不等于修复化学结构。验收和 GROMACS 输入编译通过，不代表体系已充分最小化、平衡或完成生产模拟。使用者须审核结构、力场参数和模拟设置；官网接口变化可能需要适配。

## 开发目录

`commands/` 放子命令处理，`pipeline/` 放建模流程，`remote/` 放官网访问，`chem/` 放结构规范化和化学检查，`store/` 放任务、索引、交付和会话管理，`locales/` 放双语文案。包顶层旧模块仅保留兼容导入入口。发行安装包仅放 GitHub Releases；版本历史见 [CHANGELOG](CHANGELOG.md)。

## 免责声明与负责任使用

本项目面向科研与教学，属于实验性工具，非 CHARMM-GUI 官方软件。建模结果须由使用者独立审核，不构成医疗、临床或其他专业建议，也不保证适用于任何特定目的。

使用者应自行确保具有账户、输入数据及第三方服务的合法使用权限，遵守适用法律、科研伦理和 CHARMM-GUI 等服务的使用条款。不得从事违法、未经授权的访问、绕过访问限制、损害第三方权益或干扰服务运行等滥用行为。使用者对其输入、操作、结果应用及相关后果自行负责；作者及维护者不认可或授权任何违法或不当使用。

本软件按“现状”提供，不作任何明示或默示保证。在适用法律允许的最大范围内，作者及维护者不对使用或无法使用本软件造成的损失承担责任，包括数据丢失、结果错误、服务中断及其他损害。

许可证：[GPL-3.0-only](LICENSE)。Copyright (c) 2026 X0bang.

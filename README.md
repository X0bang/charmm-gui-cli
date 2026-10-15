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

## 登录与续期

长任务可使用 `charmm-gui-cli login --remember`：密码仅保存到系统凭据库，令牌到期后重新登录 API 和网页；不重发建模 POST。服务器没有可用凭据库时，可设置 `CHARMM_GUI_PASSWORD_COMMAND` 为返回单行密码的可信命令，再执行 `login --email 邮箱`。命令和密码不写入配置，普通 `login` 不记住密码。`doctor` 显示 JWT 剩余时间（本地解码，不验证服务端状态）；`logout` 清除所选本地会话和对应记住的密码，不删除任务或撤销已发令牌。详见 [会话续期](docs/ADVANCED.md#长任务会话续期)。

## 更新

```bash
charmm-gui-cli update --check  # 只检查已发布版本
charmm-gui-cli update          # 校验下载包并更新，不删除旧安装
```

更新仅使用 GitHub Releases 的最新正式安装包，校验 SHA256；失败不切换命令入口，不修改登录会话或建模数据。旧版更新器仍使用仓库 download 路径时，请从 Releases 下载新版源码，运行一次 `bash install.sh`，或在源码目录运行 `git pull` 后重新安装。自动更新适用于本项目安装器创建的隔离安装；pip 安装请使用原安装方式升级。

## 使用

`charmm-gui-cli -h` 查看常用流程；`--help-all` 列出高级与兼容命令；`build -h` 查看输入和建模参数。

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

批量清单支持与 build 一致的 v2 分节参数，旧 v1 保持兼容；`batch --name series-a`、`sweep --name scan-a` 为容器命名。CLI dry-run 不登记全局任务列表，实际 resume 时才登记。TTY 的 batch/sweep/status/preview 显示摘要；管道和 `--json` 保持机器输出。修正未提交的本地输入失败后，可用 `batch-resume DIR --retry-failed-local`；已有部分目录或提交证据时不重建。详见 [批量流程](docs/BATCH.md)。

配体支持 SDF V2000，以及可验证保留化学图和明确立体定义的 V3000/mol2/PDBQT（规范化为 V2000，记录坐标表示舍入）。PDBQT 需 Meeko 内嵌 SMILES/索引，或 `--ligand-smiles` 与明确的原子映射/完整连接；不按距离猜键。多记录或多姿势文件须显式选择，例如 `--ligand-pose 2`；编号从 1 开始，不按分数自动选择。YAML 写作 `input_options: {ligand_pose: 2}`。mol2/PDBQT 部分电荷不等于形式电荷，预检会提示审阅。原始文件不改写，选择、转换、原子名对应和哈希写入输入证据。

脂质比例可写为 `--upper POPC:POPE=7:3`。名称先通过本地常用脂质目录检查；目录不含所有官网脂质，特殊名称核对后可使用 `--allow-unknown-lipids`（YAML：`membrane.allow_unknown_lipids: true`）。常用名称参考[官网脂质库](https://www.charmm-gui.org/?doc=archive&lib=lipid)，不代表每个 API 模块都支持全部名称。

末端补丁支持按链设置，例如 `--n-terminal A=GLYP,B=NTER`，也可用 `PROA` 等 segment 名；YAML 使用 `preparation.terminals: {A: {n_terminal: GLYP}, B: {n_terminal: NTER}}`。未指定的链使用统一默认值。

终端和报告分别展示请求设置与提交记录；例如 NTER 自动回退为 GLYP 时显示 `NTER → GLYP`。提交结果未知或只有历史记录时会明确标注，不把请求值当作已应用值。`inputs/system-config.json` 保留原始 requested/applied 字段，并新增按链整理的 `parameter_summary`。

`check` 显示失败原因、自动修复和结构审阅项，`check --full` 显示完整证据；`--json`、`--lang`、`--foreground` 可放在子命令前后。新任务本地准备失败不会占住任务名，修正输入后可以重试。

常见配置和路径错误会指出具体文件及本地复查方式，不回显 YAML 内容。直接参数错误给出的默认基线检查不替代自定义设置；修正参数后再用 `build --dry-run` 核对。

预检还会按模型、TER、链和 segment 列出组成，提示核酸、糖链及 LINK 连接，避免误把它们当作蛋白或普通可删除配体。输入已明确指定的残基状态会记录在准备证据中，不自动选择 pH 或改变原始坐标。构建时先核验官网源 PDB/PSF 中的相应原子类型、电荷和连接，再提交建膜；验收时再次核对最终组装。不能唯一对应服务器表单或无法验证状态时停止，保留证据，不降级成普通残基继续提交。

### 后台运行与恢复

`build`、`build-resume` 和 `jobs resume` 默认在后台运行。启动前在前台完成本地准备和登录检查；启动后显示任务名、PID、日志和查看命令。关闭终端不影响任务，退出 `watch` 也不会停止建模。

```bash
charmm-gui-cli jobs list
charmm-gui-cli jobs watch receptor-a   # Ctrl-C 仅退出查看
charmm-gui-cli jobs status receptor-a
charmm-gui-cli --foreground build system.yaml --name receptor-b
charmm-gui-cli --foreground jobs resume receptor-b  # 脚本/Slurm：等待并返回最终退出码
```

移动任务时保留整个目录（包括隐藏的 `.charmm-gui/`）。找不到原位置时，程序按任务身份在当前目录及其子目录查找；其他位置可显式关联：

```bash
charmm-gui-cli jobs relink receptor-a /new/path/receptor-a
```

新任务内部保存相对路径；旧记录兼容读取，不自动迁移。多个副本不会自动选择，关联也不会重新提交任务。

网页建模 POST 默认读取超时 900 秒，等待期间每 30 秒记录心跳；可用 `--post-timeout 1800` 调整。GET 单次超时不变，建模期间短暂断网会退避重试，默认最长 20 分钟；`jobs status` 可查看网络等待。可用 `CHARMM_GUI_GET_RETRY_SECONDS=600` 调为 10 分钟（范围 0–1800，0 使用短重试）。独立查询仍使用短重试，结果不明的 POST 不自动重发。

官网压缩归档时先以 20–30 秒间隔查询，再逐步退避，遵守更长的服务器等待时间。终端和 `jobs watch` 显示下载字节进度；失败响应留在 `.charmm-gui/downloads/`，不会散落在任务根目录。转换提速仍处于[等价性验证](docs/CONVERSION.md)阶段，常规建模未关闭官网转换。

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
  previews/           自动生成的 PNG 预览，不再复制一份模型坐标
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

结晶水默认删除；MSE 默认转换为 MET（Se→S，坐标不变）；altloc 按残基选择占有率最高的完整构象，保留共有原子。修复记录写入 `check.json` 和实际准备的 `inputs/input-manifest.json`，`check`、`build`、batch 和 sweep 使用同一套规则。

```bash
charmm-gui-cli check --protein protein.pdb --ligand bound-ligand.sdf --altloc B
charmm-gui-cli check --protein protein.pdb --ligand bound-ligand.sdf --drop-hetero ZN
charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --model 2 --drop-additives --name receptor-a
```

`--drop-hetero` 只能逐个指定残基名，不支持通配删除。金属/辅因子仍需明确处理：失败预检会列出位置、配位原子或最近距离，并在检查目录生成 `retained-components/` 草稿；须补全可靠 SDF、电荷及参数，草稿本身不能直接提交。`--keep-mse` 保留硒，但普通远端流程会拒绝；其他修饰残基不自动转换。跨残基 altloc 独立选择，请核对关联构象。主链 C–N 距离超过 2.0 Å、未知残基、不支持的 CGenFF 元素、配体保留名称冲突及小于 2.2 Å 的蛋白–配体接触均在本地拦截；LINK 连接/配位需要专用流程。

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

后台进程结束时，macOS 尝试系统通知，Linux 桌面在可用时使用 `notify-send`。通知不发送账户或结构内容；缺少通知服务、无桌面的服务器或发送失败均不影响任务结果。服务器上可用 `jobs watch NAME` 或 `tail -f NAME/run.log` 查看进度；暂不发送邮件或 webhook。

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

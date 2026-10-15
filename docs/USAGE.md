# charmm-gui-cli

从原始结构直接构建**膜蛋白–配体脂膜体系**，无需浏览器，也无需手工拆分结构、编写 YAML 或管理 token 和中间文件。

独立实验性客户端，非 CHARMM-GUI 官方软件。结构上传/参数化使用官网 HTTP 表单，建膜使用官方 Quick Bilayer API；官网变化可能需要适配。本程序不做 docking，不替用户选择质子化状态，不宣称输出已充分平衡。

## 1. Ubuntu 22.04+ / macOS 安装

克隆仓库并安装：

```bash
git clone https://github.com/X0bang/charmm-gui-cli.git
cd charmm-gui-cli
bash install.sh
```

也可以下载并解压发布源码包，在解压目录运行：

```bash
bash install.sh
```

也可在本项目源码目录直接运行同一命令。需要 Python 3.10+ 和 venv；缺少时安装器会给出对应系统包提示，不擅自运行 sudo/apt。首次安装联网下载依赖，包括 RDKit。软件安装到用户的 `~/.local`，不改系统 Python，不需要激活虚拟环境；升级保留旧版本。

```bash
# 如果 ~/.local/bin 尚未在 PATH 中，当前终端执行：
export PATH="$HOME/.local/bin:$PATH"

charmm-gui-cli doctor
charmm-gui-cli -h
```

真实 Ubuntu 22.04 / Python 3.10 非 root 安装已经测试。详细安装、离线 wheelhouse、自定义目录和升级说明见 [docs/INSTALL.md](INSTALL.md)。安装器不安装 GROMACS；它是可选的本地输入编译检查工具，远端建模不依赖它。

正式命令是小写 `charmm-gui-cli`，不是 `C1i`。不要把整个开发工作区当作安装包分发；发布包不包含用户结构、账号、token、Cookie、历史任务或模型归档。

## 2. 登录一次

```bash
charmm-gui-cli login
```

按提示输入邮箱和密码。程序同时建立 API 和官网会话，并保存到 `~/.config/charmm-gui-cli/`，凭据文件权限为 0600，密码不保存。换工作目录仍可使用，过期后重新 `login` 即可。

```bash
charmm-gui-cli auth
```

`auth` 默认只查看本地状态，不打印凭据。`web_session_saved` 不等于服务器会话一定仍有效。官方 token 的高级复用方式见 [docs/ADVANCED.md](ADVANCED.md)。

## 3. 提供原始结构，直接建模

### A. 蛋白 PDB + 已结合的配体 SDF（推荐）

```bash
charmm-gui-cli build \
  --protein protein.pdb \
  --ligand bound-ligand.sdf \
  --upper POPC=1 --lower POPC=1 \
  --salt KCl --salt-concentration 0.10
```

两个文件必须在**同一个结合姿势坐标系**中。配体需提供可靠的键级和形式电荷。支持 SDF V2000、可保留化学定义的 V3000 和 mol2；后两者规范化为 V2000，转换产生的坐标字段舍入会记录，不旋转、平移或优化结合姿势。程序默认按化学定义补缺失氢，不预测 pH/互变异构体。

多记录文件不能默认使用第一条，也不会按对接分数选构象；先查看预检列出的名称和分数，再明确指定记录（从 1 开始）：

```bash
charmm-gui-cli check --protein protein.pdb --ligand poses.sdf --ligand-pose 2
charmm-gui-cli build --name receptor-a --protein protein.pdb --ligand poses.sdf --ligand-pose 2
```

mol2 原子类型须可被 RDKit 识别；部分电荷与形式电荷分别报告，普通价态缺失氢也需审阅。转换不支持超过 999 个原子/键、查询结构、同位素、自由基、特殊键或增强立体组，不会静默丢弃这些定义。PDBQT 可使用 Meeko 内嵌 SMILES/索引，或 `--ligand-smiles` 提供显式 serial 映射；没有映射时必须有完整 CONECT 且图对应唯一。`--ligand-pose N` 选择其第 N 个姿势，不按分数自动选择。详见 [PDBQT 化学定义](ADVANCED.md#pdbqt-化学定义与结合姿势)。

### B. 已结合好的复合物 PDB

如果该 PDB 包含可靠的 CONECT 键级和电荷信息：

```bash
charmm-gui-cli build \
  --complex bound-complex.pdb \
  --ligand-resname LIG \
  --accept-conect-bond-orders \
  --salt KCl --salt-concentration 0.10
```

程序自动选取配体、拆分蛋白/配体、生成化学输入并建模。无需先执行 `split` 或手工生成 SDF。

**`--accept-conect-bond-orders` 是化学信息来源的明确确认，不是“忽略检查”。** 普通 PDB 往往没有可靠键级，仅凭坐标不能保证化学结构正确。没有可信 CONECT 时，不要为了通过检查而添加此选项；应使用下一种方式补充化学定义。

### C. 蛋白 PDB + 配体 PDB；或再提供结合好的 PDB

带可靠 CONECT 的配体 PDB：

```bash
charmm-gui-cli build \
  --protein protein.pdb --ligand ligand.pdb \
  --accept-conect-bond-orders
```

三个文件一起提供：

```bash
charmm-gui-cli build \
  --protein protein.pdb --ligand ligand.pdb \
  --complex bound-complex.pdb --ligand-resname LIG \
  --accept-conect-bond-orders
```

提供 `--complex` 后，最终结合坐标取自复合物，不使用独立配体的游离构象。蛋白输入必须与复合物中的蛋白身份和坐标一致；独立配体的原子名称、元素和连接必须可明确对应，不会忽略不一致的文件。

若 PDB 有连接图、但没有可信键级，可提供 `--ligand-smiles '...'` 定义键级和电荷。要求元素/连接图到模板存在唯一映射；无连接图或对称性歧义时不会按距离猜键、不会任意选择第一种映射。指定立体化学的复杂转移请用已结合的 SDF。

### D. 复合物 PDB + 明确命名的化学 SDF

```bash
charmm-gui-cli build \
  --complex bound-complex.pdb --ligand chemical.sdf \
  --ligand-resname LIG
```

此时坐标取复合物，化学定义取 SDF。SDF 必须含 `PDB_ATOM_NAMES` 属性，按 SDF 原子顺序列出对应 PDB 原子名；名称唯一，原子集合及元素一致（包括显式氢）。复合物不含 CONECT 时，使用明确提供的 SDF 化学图；若有 CONECT，则必须一致。未对应的氢、模糊映射和未经核对的立体化学转移会明确拒绝。

不能提供可靠映射时，直接使用方式 A 的已结合 SDF；工具不会把名字相似当作化学对应已成立。

## 4. 参数及默认行为

| 参数 | 默认值 / 含义 |
| --- | --- |
| `--upper` / `--lower` | `POPC=1`；也可 `POPC:CHL1=3:1`，为比例而非精确分子数 |
| `--salt` | `KCl`；直接模式自动验收支持 KCl、NaCl |
| `--salt-concentration` | 0.15 mol/L |
| `--margin` | 20 Å，膜侧向边界 |
| `--water-padding` | 22.5 Å，Z 方向水层边界 |
| `--orientation` | `ppm`；`prepared` 要求输入已有正确膜取向 |
| `--n-terminal` / `--c-terminal` | NTER / CTER，统一应用于蛋白链 |
| `--hydrogens` | `add_missing`；可用 `preserve` 保留已有氢 |
| `--ligand-resname` | LIG；必须明确选择一个配体残基 |
| `--out` | 可省略，自动创建任务目录；指定时必须是新目录 |
| `--name` | 可选的唯一任务名；使用 `jobs status/resume/report NAME` |
| `--foreground` | 等待本地流程返回结果退出码；默认后台运行，用 `jobs watch NAME` 查看 |
| `--interval` | 常规轮询间隔，秒；默认 30、最小 10；退避或服务器 Retry-After 可延长 |
| `--max-wait` | 本地等待循环的轮询预算，秒；默认 21600（6 小时），不是远端取消或硬性总超时 |
| `--dry-run` | 仅本地准备和检查；不登录、不上传，可随后恢复 |
| `--grompp` / `--no-grompp` | 默认检测到 gmx 就编译；前者要求必须编译，后者跳过编译 |
| `--gmx` | 默认 `gmx`，可指定可执行文件路径 |

这些默认参数不是针对任意膜蛋白的科学建议。多配体/有机辅因子使用实验性 `--components` 入口，见 [组分说明](COMPONENTS.md)；不支持共价配体、复杂金属配位、自动缺失片段修补或自动 pH 选择。默认删除结晶水，按完整性和占有率选择 altloc，转换 MSE→MET，并保守补全可推断的元素和链 ID；修复会记入检查与输入证据，原文件不改。多模型须显式选择；其他金属、辅因子和未知残基仍需明确处理。

过近或远离的配体会被拒绝，例如蛋白–配体重原子距离小于 2.2 Å 或单配体输入整体远于 6 Å。小于 0.8 Å 属于严重重叠，2.2 Å 内可能是共价连接或坐标错误；当前流程不支持共价配体。

环境验收检查请求的脂质种类、水、离子种类及浓度直接证据。0.4.0 进一步核对上下叶层的最终脂质种类、数量与请求比例，证据来自尺寸流、packing head、最终坐标及 PSF；每种允许一个分子的离散舍入偏差。支持明确 P 头基的磷脂和 CHL1/O3；缺少可靠证据、未知头基或改变过的坐标约定会阻断验收。SMILES 转换还会拒绝同位素、自由基和配位键，避免悄悄改变化学定义。

## 5. 自动任务管理与结果

默认 `build` 自动完成：

输入检查/化学转换 → 补氢与合并 → 上传 → CGenFF 参数化 → PPM/脂膜构建 → 下载 → 拓扑、环境和相对位姿验证。

处理进度显示在 stderr；终端 stdout 显示易读摘要，管道或 `--json` 输出机器可读 JSON。结果给出任务、归档和完整报告位置。安装了 GROMACS 时自动使用原始最小化 MDP 执行 `grompp -maxwarn 0`，不运行 MD；未安装时会明确显示未编译。

```bash
# 最近任务，无需记住中间文件路径
charmm-gui-cli jobs
charmm-gui-cli build-resume

# 多任务时，也可指定任务目录
charmm-gui-cli build-resume /path/to/run

# 默认后台运行，按需查看日志
charmm-gui-cli build --name receptor-a --protein protein.pdb --ligand bound.sdf
charmm-gui-cli jobs watch receptor-a
```

建模默认在后台运行，任务只占当前目录下的 `./<任务名>/`，`--out DIR` 可指定其他位置，日志为该目录的 `run.log`；用 `jobs watch NAME` 跟踪，Ctrl-C 只退出查看。脚本和 Slurm 使用 `charmm-gui-cli --foreground build ...` 获取最终退出码。旧任务位置不迁移，`jobs` 仍通过全局索引查找。主要结果是：

- `charmm-gui.tgz`：完整 CHARMM-GUI 模型归档，仅保留一份。
- `model/`：解压后的模型和模拟输入，仅保留一份。
- `report.txt`：可直接阅读的验收结论；详细证据保存在 `.charmm-gui/results/validation.json`。
- `inputs/`：实际使用的输入，`system-config.json` 区分 requested 和 applied 参数。

程序不会覆盖已有任务或删除原始结构。新任务的本地准备失败时，仅撤销本次创建的暂存内容，可修正后使用同名重新建模；远端任务、提交意图和未知 POST 不会因此被清除。报告缓存核对归档、实际模型、输入、配置和编译产物。升级后可查看历史报告及其原验收版本；显式重新验收使用当前版本。

模型解压完成后才交付为 `model/`，中断可恢复。修改模型会使旧验收结论失效。新任务原地更新报告时，用户编辑过的 `report.txt` 和 `README.txt` 会先保存为 `.user-edited`（必要时带编号），再生成报告；这些备注不影响验收证据。修改过的模型、`inputs/system-config.json` 和已有独立导出副本仍不会被覆盖，需自行保留修改或导出到新的 `--to` 目录。

等待循环默认轮询预算 6 小时，常规查询间隔 30 秒；`--max-wait` 和 `--interval` 可调整。同步 POST、退避和本地验收不受这个预算硬性打断，预算到时也不取消远端任务；使用 `jobs resume NAME` 继续同一任务。

后台启动返回码 0 只表示已启动；`jobs status NAME` 查看当前状态。前台模式返回码 0 也可能是 `--no-wait` 下的运行中状态。完整完成需看到 `state: validated`；2 表示错误或验证失败，130 表示中断或退出查看。

`--wait/--no-wait` 是保留的兼容选项，常用帮助不再展示。它们与 `--foreground` 作用不同：`--no-wait` 只推进一轮，可能在建膜仍运行时返回，之后要显式 resume；不建议用它代替默认后台运行。`--foreground` 在默认等待模式下才适合脚本等待最终结果。

## 6. 本地预检与恢复

主链断裂默认阻断。明确接受独立片段时，给 `check` 和后续 `build` 都加 `--split-chain-breaks`；配置文件用 `input_options.split_chain_breaks: true`。先查看生成的链标识，再设置逐片段末端；这不会补齐缺失片段。详见[片段设置](ADVANCED.md#显式分离主链断裂)。

```bash
charmm-gui-cli build \
  --complex bound-complex.pdb --accept-conect-bond-orders --dry-run
charmm-gui-cli login
charmm-gui-cli build-resume
```

`--dry-run` 不上传，保存准备好的本地任务。之后恢复会提交该任务，不需要重新处理输入。

遇到登录过期：`login` 后 `build-resume`。遇到写请求响应不确定，程序保留提交意图，不盲目重发以免产生重复任务；此类异常可能仍需人工核对远端任务，不能保证所有网站异常都自动恢复。

## 使用配置文件建模

不想每次输入膜组成、盐浓度等参数时，可使用 [默认 YAML 模板](../examples/default-build.yaml)。先复制或下载到自己的项目目录，命名为 `system.yaml`，修改 `protein`、`ligand` 路径及模型参数，再运行：

```bash
# 只做本地准备，成功后再继续同一任务
charmm-gui-cli --lang zh build system.yaml --name receptor-a --dry-run
charmm-gui-cli login
charmm-gui-cli jobs resume receptor-a
```

若不需要分步检查，登录后直接使用 `charmm-gui-cli build system.yaml --name receptor-a`；两种方式二选一。相对结构路径以 YAML 所在目录为准。上层 POPC:POPE = 7:3 写为 `upper: "POPC:POPE=7:3"`，下层单独配置。

任务创建后恢复使用保存的配置，修改原 YAML 不会改变该任务；需要另一组参数时使用新任务名。配置不能与直接结构/模型选项混用，`--name`、`--out` 等运行选项仍放在命令中。原 0.5.2 源包尚无该模板，可以单独下载；下载位置、完整配置、参数说明及恢复步骤见 [配置文件建模教程](ADVANCED.md#用-yaml-配置文件建模)。

## 7. 科学验证边界与已完成测试

底层真实测试已完成 CrtW–β-carotene、POPC、KCl 0.10 M 建模：367489 原子、942 POPC、78620 水、143 K⁺ 和 144 Cl⁻；配体 40 个重原子及完整连接图保留，最终蛋白拟合后的配体 RMSD 0.308 Å，GROMACS 2024.4 `grompp -maxwarn 0` 通过。盐条件错误的旧模型被正确拒绝。

原始复合物直接建模复测也已完成。0.4.0 对两份独立真实归档完成叶层、参数评分、CHARMM 日志和 GROMACS 验收；批次复用两份已完成归档的恢复验收也通过。详见 [测试记录](TESTING.md)。

**验证通过不是生产模拟认证。** 服务器短最小化仍有 bent-improper 警告，未证明充分收敛或平衡。本次输出使用 HMR、生产 MDP 为 4 fs 和 303.15 K；这些不是本工具保证不变的默认值。需审查质子化、取向、配体参数、质量重分配、约束和完整平衡流程。报告始终保留 `scientific_correctness_verified: false`。

证据和兼容性修复见 [RESEARCH.md](../RESEARCH.md)。测试记录、目录清单及发布验收见 [docs/TESTING.md](TESTING.md)。

## 8. 开发者与高级用法

```bash
python -m pip install -e '.[chem]'
python -m unittest discover -s tests -v
python3 scripts/build_release.py --out dist/new-release
```

YAML、官方 token、自定义会话、已准备 job ID 及低层诊断见 [docs/ADVANCED.md](ADVANCED.md)。发布包不含私有 CrtW PDB，因此依赖该文件的集成测试会明确跳过；合成数据测试继续运行。

命名任务、错误分类及恢复操作见 [恢复说明](RECOVERY.md)；多体系清单、并发和汇总见 [批量建模](BATCH.md)。

```text
charmm_gui_cli/   CLI、输入化学、认证、HTTP/API、任务管理、验证
tests/            离线回归测试
scripts/          用户级安装与白名单发布构建
install.sh        一键安装入口
docs/             安装、高级用法、完整性及测试说明
examples/         高级 YAML 示例
README.md         从安装到建模的主手册
RESEARCH.md       接口调研及真实建模证据
```

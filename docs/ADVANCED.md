# 高级配置、认证与恢复

常规使用只需 `login` 和 `build --protein ... --ligand ...`，见 [README](../README.md)。本页面向需要保留 YAML、官方 token 或已有 job ID 的用户。

## 用 YAML 配置文件建模

把结构路径、膜组成、盐浓度等参数写在 `system.yaml` 中，每次修改配置后传给 `build`。不需要手写 token、job ID 或中间文件；从本地蛋白和配体开始请用 `version: 2`。

### 1. 获取模板并准备输入

在自己的新项目目录中放好蛋白 PDB 和结合姿势配体 SDF。如果从最新仓库源码开始，可复制 [默认模板](../examples/default-build.yaml) 到项目目录，命名为 `system.yaml`。

原 0.5.2 发布源码包不含后来新增的模板；只安装了程序或使用该源码包的用户，可单独下载。以下命令在自己的项目目录执行，先确认没有同名配置，避免覆盖：

```bash
curl -fL https://raw.githubusercontent.com/X0bang/charmm-gui-cli/main/examples/default-build.yaml \
  -o system.yaml
```

推荐目录布局：

```text
my-model/
├── system.yaml
├── protein.pdb
└── bound-ligand.sdf
```

两个结构必须已处于同一个结合姿势坐标系；配体需带可信键级及形式电荷，可使用 SDF V2000、可保留化学定义的 V3000 或 mol2。多记录文件在配置中用 `input_options: {ligand_pose: 2}` 明确选择第二条，编号从 1 开始；不自动按分数选取。mol2 部分电荷不能替代形式电荷，需要审核转换报告。配置文件不能替代分子对接或化学定义。程序不会自动寻找默认配置，必须显式传入文件。

### 2. 编辑 system.yaml

默认模板上下膜叶为纯 POPC、KCl 0.15 M。下面是改为**上层 POPC:POPE = 7:3、下层纯 POPC、KCl 0.10 M**的完整示例：

```yaml
version: 2
protein: protein.pdb
ligand: bound-ligand.sdf
ligand_resname: LIG
ligand_hydrogens: add_missing
preparation:
  n_terminal: NTER
  c_terminal: CTER
  orientation: ppm
membrane:
  upper: "POPC:POPE=7:3"
  lower: "POPC=1"
  margin_A: 20
  water_padding_A: 22.5
ions:
  type: KCl
  concentration_M: 0.10
```

可在同一个版本 2 配置中加入输入选择选项；直接 CLI 的对应参数不能与配置文件混用：

```yaml
input_options:
  altloc: highest       # 或 A/B；必须是完整构象
  drop_hetero: [ZN]     # 仅明确不保留时填写，逐个残基名
  drop_additives: false
  keep_mse: false       # 默认 MSE→MET；true 需要专用参数流程
  model: 1             # 仅需选择多模型文件时填写，从 1 开始
```

上述选项也可直接放入 batch 的 `defaults`/成员，以及 sweep 的 `inputs`。结晶水默认移除，原始文件不改，实际输入和修复记录在任务的 `inputs/` 中。预检遇到金属或辅因子时，检查目录中的 `retained-components/components.yaml` 标出待补全的化学定义；它不是可直接运行的参数文件。金属配位和修饰残基补丁仍需要专用流程，不能仅把草稿的 `trusted` 改为 `true`。

蛋白 PDB 已经夹带配体、又传入独立 SDF/mol2 时，程序会检查重原子是否在原坐标系中一一对应（RMSD < 0.5 Å，不做拟合）。这只是可能重复的结合姿势，不证明化学身份、键级或电荷一致；不会自动删除。确认使用独立配体定义后，可显式使用 `--drop-hetero LIG`。若同名残基还有其他不吻合的结合位点，提示会说明批量删除的范围，不再建议按名字直接删除。比对有计算上限，达到上限仍保留原来的组分拒绝，不把未判定组分视为安全。

列错位 PDB 会指出原始行号及字段，不再误报原子序号重复。固定列依据 [wwPDB 坐标格式](https://www.wwpdb.org/documentation/file-format-content/format33/sect9.html)；CHARMM 的四字符残基名和已支持的缺列修复仍可用。插入码独立保留，编号未使用不代表序列缺失。明确 `--model N` 后只要求所选模型可解析；未选模型的原始清点失败写入 `check.json`，不会阻止有效模型。

当前不读取 [hybrid-36 编号](https://cci.lbl.gov/hybrid_36/)或 mmCIF，超出传统 PDB 列宽会明确拒绝，不截断成另一个编号。需要用结构编辑器拆分或明确重编号，保留坐标、编号对应与 LINK/SSBOND/CONECT 信息，再运行提示中的实际 `check` 命令。

`check` 按模型、TER 块、链 ID 和 segid 列出残基组成及坐标中的首末编号；首末编号不表示序列连续或完整。JSON 的 `protein_inventory.structure_inventory` 保留全部条目，终端最多显示 20 条。相同清点信息保存在实际准备输入的 `input-manifest.json` 中；`build --dry-run` 也显示链组成。程序不会自动删除融合结构域、抗体或纳米抗体链，也不会根据链长猜测身份。[COMPND](https://www.wwpdb.org/documentation/file-format-content/format33/sect2.html#COMPND) 中的分子/片段名只作为输入文件自述展示，未经序列鉴定。

混入的常见核酸残基会被明确拒绝，不按氨基酸上传或计算肽链缺口。糖类仅按本地残基名表给出候选提示，不是完整的化学组分词典；例如 [NAG](https://files.rcsb.org/ligands/view/NAG.cif) 的名称不能单独证明已正确建模糖基化。若存在 [LINK](https://www.wwpdb.org/documentation/file-format-content/format33/sect6.html#LINK)，会列出连接端点；糖基化、跨组分共价连接及配位需要专用拓扑流程。仅删除糖或 LINK 并不能证明剩余体系适合非共价建模。

单配体输入出现多份同名配体时，会列出前 10 个结合位点身份；TER 隔开的实例不会合并。组分配置若不能区分同一身份在多个 TER 块中的实例，也会拒绝，需另设唯一标识并保留编号对应。当前远端验收仍不支持同名多拷贝配体，需明确选择目标复合物，不能任取第一份。预检摘要显示每个已定义配体的形式电荷，并提示核对预期质子化状态；`add_missing` 保留已定义电荷，只按价态补氢，不选择 pH 或推断蛋白/整个体系的电荷。

本地蛋白状态处理会记录输入已声明的状态，而不预测 pH：[官网 FAQ](https://www.charmm-gui.org/?doc=faq) 要求 Amber 的 HID/HIE/HIP 使用 CHARMM 的 HSD/HSE/HSP 名称。[蛋白拓扑](https://www.charmm-gui.org/?doc=open_toppar&filename=top_all36_prot.rtf) 中 ASPP、GLUP、LSN 是补丁，不是独立残基模板。因此 ASH/GLH/LYN 的上传基础残基写成 ASP/GLU/LYS，目标补丁另记在 `input_normalization.protein_state_intent`。原始文件和坐标不改；LYN 的显式氢若需要改名，会逐项记录，不静默增删氢。显式氢与声明状态冲突时拒绝。

CYX 只在 SG–SG 距离 1.8–2.3 Å 且配对唯一时记录 DISU 候选；普通 CYS 不会仅因靠近就被改为二硫键。缺 SG、元素不符、多配对或与 SSBOND 冲突时拒绝。状态请求会参与前处理复用身份，避免将坐标相同的 ASP 与 ASH 混用。远端实际 PDB/PSF 的状态验证尚未接通，因此 `check`/`--dry-run` 能记录请求，但 `build` 不提交这些状态配方，也不把本地改名或归档存在视为状态已实现。此限制同样适用于输入已使用 HSD/HSE/HSP 等显式状态名称的情况。

| 字段 | 如何设置 |
| --- | --- |
| `protein` / `ligand` | 改为自己的文件路径；相对路径以 YAML 所在目录为基准，也可用绝对路径 |
| `ligand_resname` | 配体准备后的残基名，1–3 位大写字母/数字且以字母开头，如 `LIG` |
| `ligand_hydrogens` | `add_missing` 按已有化学定义补氢；`preserve` 不补氢，都不预测 pH |
| `preparation.n_terminal` / `c_terminal` | N/C 端补丁，需根据实际蛋白构建体审核 |
| `preparation.orientation` | `ppm` 处理膜取向；`prepared` 要求输入已有正确膜取向 |
| `membrane.upper` / `lower` | 模型上/下膜叶；混合膜写成 `"POPC:POPE=7:3"`，使用英文冒号，比例均为正数 |
| `membrane.margin_A` / `water_padding_A` | 侧向边界和 Z 方向水层边界，单位 Å |
| `ions.type` / `concentration_M` | 例如 `KCl`、`NaCl`；浓度单位 mol/L |

使用空格缩进，不用 Tab。脂质比例是相对分子数比例，不是分子总数；最终整数数量可能取整。上/下膜叶不自动等同于生物学外/内层，需要核对蛋白取向。模型默认值不是科学推荐；不应在 YAML 中放邮箱、密码、token 或 Cookie。

### 3. 本地准备并检查，再继续建模

```bash
# 只做本地准备和检查，不登录、不上传
charmm-gui-cli --lang zh build system.yaml --name receptor-a --dry-run

# 上一步成功后登录，然后继续同一任务
charmm-gui-cli login
charmm-gui-cli --lang zh jobs resume receptor-a
```

`--dry-run` 会创建并保存任务，不只是查看参数。恢复时使用任务保存的配置，不会重新读取后来修改的原 YAML。若要测试另一组参数，先编辑配置，再以新任务名（例如 `receptor-b`）创建任务；不要删除或手改中间文件，也不要在已提交任务上强行换参数。

不需要分步检查时，登录后直接执行：

```bash
charmm-gui-cli login
charmm-gui-cli --lang zh build system.yaml --name receptor-a
```

这两种启动方式二选一。已创建 `receptor-a` 时使用 resume，不要再用相同任务名重复 build。0.6.0 默认由后台进程等待、下载并验收，终端立即返回启动信息；`jobs watch receptor-a` 跟踪日志。脚本或 Slurm 使用 `charmm-gui-cli --foreground build ...` 等待最终退出码。`--no-wait` 只推进一轮，之后需恢复。

### 4. 查看进度、结果和中断恢复

```bash
charmm-gui-cli jobs status receptor-a
charmm-gui-cli jobs watch receptor-a
charmm-gui-cli jobs resume receptor-a
charmm-gui-cli jobs report receptor-a --format text
charmm-gui-cli jobs report receptor-a --format html --out receptor-a.html
```

HTML 输出文件需尚未存在。网络中断或 Ctrl-C 后恢复原任务，不重新 build；提示提交结果不明时先核对远端任务，不盲目重发。`validated` 才表示自动验收完成；官网 Done 不代表已经下载、验收、平衡或适合生产模拟。

### 5. 支持范围与常见问题

输出目录可省略，程序自动分配；也可用 `--out` 指定尚不存在的任务目录。`--name`、`--out`、`--no-wait`、`--grompp`、`--no-grompp` 等运行选项放在命令中，不写成 YAML 字段。全局选项 `--lang`、`--json`、`--foreground` 可放在子命令前后。

不要把 YAML 与 `--protein`、`--ligand`、`--upper`、`--lower`、`--salt` 等直接结构/建模参数混用，否则报错。要调整参数，请改 YAML；未支持的字段、重复字段、缺失文件或不合法数值也会报错。

当前 `version: 2` YAML 支持独立蛋白 PDB 加配体入口，不支持把所有 CLI 选项任意搬进配置；复合物选择、SMILES、`--components` 多组分等需使用对应直接入口/组分清单。批量或参数扫描分别使用 [批量清单](BATCH.md) 和 [扫描清单](SWEEP.md)。

版本 1 的 `plan/run/resume` 只用于已完成 PDB Reader/CGenFF 的源任务，不能上传本地结构，不要与版本 2 的 `build/build-resume` 混淆。版本 1 的 `ligand_parameters_ready` 是用户声明，不是远端准备成功的证据。

## 官方 token 与自定义登录位置

### 长任务会话续期

默认 `login` 只保存 token 和 Cookie，不保存密码。需要后台任务、batch 或 sweep 到期后自动继续时，先明确启用其中一种方式：

```bash
charmm-gui-cli login --remember
```

密码保存在系统凭据库，接受内置 macOS 钥匙串、Linux Secret Service/KWallet 等系统后端，不回落到明文或自行加密的文件。系统凭据库须可用且已解锁；完整安装器包含 `auth` 依赖，从源码仅用 pip 安装时可执行 `python -m pip install '.[auth]'`。后端和接口说明见 [keyring 官方文档](https://keyring.readthedocs.io/en/latest/)。

无图形界面或系统凭据库不可用时，可用自己的可信秘密管理器命令：

```bash
export CHARMM_GUI_PASSWORD_COMMAND='/absolute/path/to/password-helper'
charmm-gui-cli login --email your-email@example.org
```

命令应只向 stdout 返回单行 UTF-8 密码，退出码为 0；最多 4096 字节，超时 30 秒。不经过隐式 shell，不提供交互 stdin，也不记录命令、stdout 或 stderr；不要在命令参数中硬编码密码。需要管道时，在自己受控的辅助脚本中实现。环境变量须在 worker/Slurm 的实际环境中保留；辅助命令本身由用户信任和维护。

必须先完成一次 `login` 建立账户记录；单独设置密码命令不会从旧 token 猜测账户。环境变量 `CHARMMGUI_TOKEN` 仍优先于默认文件，不自动续期；自定义 token 文件只使用与其账户记录绑定的 Cookie 和凭据库。API 与网页均确认登录后才保存成对文件，原 session 格式和 0600 权限不变，新增账户记录不包含密码或命令。Cookie、账户或文件被外部改动时，停止自动使用并要求重新登录。

续期发生在已知未提交的预检或只读请求中；已发出的建模 POST 失败仍保留不确定提交记录，不自动重发。多个进程通过会话锁共享一次续期；失败会有短暂冷却。`jobs reconcile` 不自动登录，仍只做远端 GET，过期时先单独运行 `login`。旧 `web-login` 不能覆盖成对会话，请用完整 `login`，或显式指定独立 Cookie 文件。

```bash
charmm-gui-cli doctor
charmm-gui-cli logout
charmm-gui-cli --token-file /path/to/account.token logout
```

`doctor` 只读本地 JWT 有效期，不访问凭据库或服务器；不足两小时会在启动建模时提醒。`logout` 仅清除所选 token、Cookie、账户记录以及对应的记住密码；锁文件和非秘密的失败冷却记录可保留，模型/日志/任务索引不动，也不撤销服务端令牌。系统凭据库删除失败时会明确报错，不伪称退出成功；解锁后重试。若设了 `CHARMMGUI_TOKEN`，还须自行 `unset CHARMMGUI_TOKEN`。

### 官方 JWT 和自定义会话路径

官方 JWT 可以按相同 Bearer 协议使用，但不等于网站 Cookie。从原始结构建模需要同一账户的两种会话，默认 `login` 自动处理。

```bash
charmm-gui-cli login --save-to /path/to/account.token
charmm-gui-cli --token-file /path/to/account.token build system.yaml
```

默认 Cookie 写到 token 同目录的 `session.cookies.json`；可以用 `--cookies` 覆盖。已有官方 token 时：

```bash
charmm-gui-cli web-login --cookies /path/to/account.cookies.json
charmm-gui-cli --token-file ~/.charmmgui_token build system.yaml \
  --cookies /path/to/account.cookies.json
```

`--token-file` 可放在子命令前后。凭据优先级为：显式文件 → `CHARMMGUI_TOKEN` 环境变量 → 全局会话 → 旧版当前目录 `session.token` → `~/.charmmgui_token`。选中的凭据失效不会静默换账户。环境变量 token 默认配全局 Cookie，用户必须确保同账户；无法确认时使用显式路径或默认 `login`。

```bash
charmm-gui-cli auth --check --jobid 1234567890
charmm-gui-cli status 1234567890 --diagnostic
```

`auth` 默认只解析本地 JWT 格式/有效期，不验证签名；`--check` 需要真实任务 ID，用只读远端访问验证可用性。`status=null` 的新上传任务不代表准备完成。

## 独立验证或补 GROMACS 检查

常规 `build/build-resume` 已自动验证，无需找中间路径。以后安装 GROMACS 后，对任务补充编译检查：

```bash
charmm-gui-cli build-resume /path/to/run --grompp --gmx /path/to/gmx
```

独立导入归档时使用底层命令：

```bash
charmm-gui-cli validate /path/to/charmm-gui.tgz \
  --input-manifest /path/to/run/inputs/input-manifest.json \
  --build-config system.yaml \
  --reference-pdb /path/to/run/inputs/complex.pdb \
  --out /path/to/new-validation --grompp --gmx gmx
```

此独立命令的 `--build-config` 仍要求原蛋白/配体路径可访问；自动建模的最终验证直接使用任务中保存的输入与配置，无需用户重建 YAML。找不到浓度证据或参数不匹配会失败。位姿检查按蛋白 Cα 刚体拟合后比较配体，默认阈值蛋白 1 Å、配体 2 Å，不独立拟合配体，也不自动解包周期边界。

## 不确定提交的保守恢复

写请求发出后网络中断，不一定代表服务器没创建任务。程序保留 intent，不自动重发，也不允许删掉 intent 强行再试。如果核对官网任务记录后确定 API 建膜 job ID：

```bash
charmm-gui-cli attach /path/to/run/bilayer --jobid 2345678901
charmm-gui-cli build-resume /path/to/run
```

`attach` 只能绑定尚未记录 ID 的 API 建膜提交，不能恢复未知 ID 的原始上传；访问成功也不能证明该 ID 就属于原请求，需人工核对。正常流程不需要浏览器，异常身份/页面变化可能需要人工处理。

`web-inspect/web-upload/web-step` 是 HTTP 表单诊断入口，不是稳定的官方 REST API。不要公开 HTML 快照、会话文件或整个运行目录。

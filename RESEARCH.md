# 认证与膜蛋白–配体 API 调研

调研日期：2026-10-01，持续实测更新至 2026-10-02。源码对象：[官网链接的 cgui_api](https://github.com/junepark6/cgui_api)，固定提交 `30ed9b2efd64d5d4e21c84524bc9b552c0094fe7`。本项目独立实现，没有复制其代码；本次检出的上游仓库未发现 LICENSE 文件，故没有直接 fork 后修改/分发其实现。

## 1. Token 是否能跨客户端使用

> 当前结果：第 9 节记录已通过的真实端到端验收。第 5–8 节中的“待验证/进行中”保留为开发阶段历史，不代表当前完成状态。

> 本文中的 runs/、test-dataset/ 等证据路径仅在开发工作区存在。安装/发行包不携带用户结构、模型或会话文件；0.3.0 用户工作流和安装验收见 README.md 与 docs/TESTING.md。

可以按相同协议使用。[上游 client.py](https://github.com/junepark6/cgui_api/blob/30ed9b2efd64d5d4e21c84524bc9b552c0094fe7/python/charmmgui/client.py) 的 `login()` 发送邮箱/密码到 `/api/login`，从 JSON 取 `token`；`_headers()` 只构造 `Authorization: Bearer <token>`，没有客户端密钥、应用注册、SDK 签名或网页 Cookie。新客户端可发送同一 token。

这证明客户端协议兼容，未证明某个具体 token 当前有效，也没有排除服务端额外的访问规则。`auth --check --jobid ...` 用实际只读请求检验访问；本地 JWT 解码不校验签名。

常见失败应分别诊断：token 已过期、误传 token 文件路径、把整个登录 JSON 当作 JWT、复制了网页登录 Cookie、任务属于其他账户、网络代理返回 HTML、重定向导致 Authorization 丢失。服务器 403 本身不能精确区分所有原因。

## 2. 网页认证与 API 认证

现场未登录 GET `https://charmm-gui.org/api/check_status?check_rq=true` 返回 HTTP 401，JSON 内容为 `{"error":"No token provided"}`；Content-Type 却是 `text/html`，因此客户端不能仅凭 MIME 类型判定是否 JSON。

初次调研读取公开输入页面后得到登录表单：POST `?doc=sign`，字段包括 `do=login`、`email`、`password`，页面明确说明用 Cookie 保存会话。当时没有进行账户登录，也没有读取用户浏览器 Cookie。API JWT 不应直接当作网页会话。2026-10-02 已补充同账户 HTTP 登录实测，见第 7 节。

公开依据：[API 通用文档](https://www.charmm-gui.org/?doc=api)、[网页登录页面](https://www.charmm-gui.org/?doc=sign)。

## 3. 建模接口的证据边界

[Quick Bilayer 文档](https://www.charmm-gui.org/?doc=api&module=quickb) 明确要求有蛋白的体系从 PDB Reader job ID 开始，并公开 `heteroatoms`、`clone_job`、`ppm` 等参数。文档没有给出足以实现本地复合物上传、配体选择/参数化的完整调用序列。

`heteroatoms=true` 为尝试保留非蛋白组分提供接口依据，但没有证据表明任意配体都能被此接口参数化。必须从准备好的任务出发，并检查实际输出。旧版 `version: 1` 配置中的 `ligand_parameters_ready` 是用户声明；新版 `build` 则检查实际前处理页面状态及当前任务的 PDB/PSF/配体参数链接后才进入建膜，仍须核对下载的实际参数与最终体系。

[HTS 官方膜体系教程](https://www.charmm-gui.org/?doc=demo&id=hts&lesson=2) 展示同一 GPCR 与五个配体复合物的批量建膜，并包括配体参数化、PPM 取向、组装和 MD 输入生成。这确认网页能力，但不构成 HTS 公开 API 的证据。

## 4. 上游源码问题与本版选择

- [cli.py](https://github.com/junepark6/cgui_api/blob/30ed9b2efd64d5d4e21c84524bc9b552c0094fe7/python/charmmgui/cli.py) 实际布尔参数使用下划线（如 `--run_ppm`），README 部分示例用连字符；Python 提交的 `run_ppm` 与网页文档/Bash 使用的 `ppm` 不一致。
- 同一 CLI 将命令行参数字典仅删除 `cmd` 后传入提交层，包含非建模参数；也没有公开 `heteroatoms` 开关。本版只发送显式映射的字段。
- API 文档使用 `Ion_conc`/`Ion_type`，上游脚本使用小写 `ion_conc`/`ion_type`。初版按文档发送；2026-10-02 实测发现大写开头字段没有产生请求的盐条件。改用小写后，新任务生成的离子构建文件已确认正确的 KCl 0.10 M 设置；最终坐标与完整验证仍待完成，详见第 8 节。
- 上游 `download()` 以 Content-Type 包含 `application` 判断可下载，且未先检查 HTTP 状态；因此某些 JSON 错误响应可能被写成 tgz。本版检查 HTTP 错误和 gzip tar 格式，不自动解压。
- 上游 `setup.py` 只有 `setup()`，检出目录没有提供相应项目元数据/入口配置；不能仅依据 README 保证安装后能获得 `charmmgui` 命令。本项目提供完整 `pyproject.toml` 和独立命令名。

以上是该固定提交的源码观察，不代表其他版本或服务端一定存在相同问题。

## 5. 初始验证记录（2026-10-01）

- 已验证：源码认证方式；未登录服务端的 401；网页登录公开表单；本地模拟测试与 CLI 安装/运行。
- 未验证：真实账户登录；现有 token 的服务器可用性；带配体的真实 Quick Bilayer 提交；盐参数大小写；源任务准备阶段兼容性；下载的科学有效性。
- 没有使用真实凭据，没有创建远端建模任务，没有调用未公开的配体参数化接口。

另对用户提供的 `test-dataset/CrtW_BetaCarotene_af3_fixed.pdb` 做了离线检查，SHA-256 为 `4554685d0287d703597474a669dad85021fb3f027c0b74bda29579ddeee8ae16`。蛋白 A 链 320 残基，配体 `LIG B 0` 有 40 个碳原子；未修改输入。生成的前处理报告不包含已完成远端参数化的声明。

真实验收应使用一个已准备好的膜蛋白–配体体系，确认任务可访问后提交克隆任务，核对最终结构、配体参数、膜取向、离子和目标引擎输入。残基名检查只是第一层筛查。

## 6. 真实登录测试（2026-10-02）

用户提供测试账户后，通过本客户端的 `/api/login` 成功获取 JWT，并写入本项目的 `session.token`（0600，Git 忽略）。账号、密码和 token 内容未写入本报告或代码。

携带该 token 请求 `/api/check_status?check_rq=true` 时，服务器返回 HTTP 200、`text/html` 和 PHP Notice `NO JOBID!!`；这与上游客户端的无 job ID 查询实现不兼容，不能当作成功查询任务。已将本工具改为明确要求 job ID，并增加回归测试。

已确认本工具能使用官方登录接口取得标准 token；具体任务访问及真实建膜尚未验证，因为没有已完成前处理的测试 job ID。没有提交新的远端建模任务。

## 7. HTTP 前处理与输入式 CLI（2026-10-02，进行中）

本节更新第 5/6 节的历史验证边界；此前“未登录/未提交”的描述仅对应当时阶段。

- 已使用 Python requests 直接登录官网表单并保存独立 Cookie 会话，无需浏览器，也没有读取用户浏览器 Cookie。API JWT 与 Cookie 可来自同一个账户，但两者不能互换。
- `login --web` 一次输入密码即可建立 API 与 HTTP 会话；`web-login` 可单独更新 Cookie；两种凭据以 0600 保存，密码不写文件。
- 已通过 HTTP 上传 CrtW 复合物并完成真实 CGenFF 前处理，取得源 PDB/PSF 与配体 RTF/PRM。前处理成功后再把源 job ID 交给 Quick Bilayer，而不是假设 `heteroatoms=true` 会自动生成配体参数。
- 增加 `version: 2` 输入配置、`build` 和 `build-resume`：独立蛋白 PDB + 单分子 SDF → 本地化学/坐标检查 → HTTP 上传与参数化 → 官方建膜 API → 下载及后续验证。`version: 1` 的已准备 job ID 路线继续保留。
- 写请求先保存独占提交意图；POST 遇到 307/308 不重发。来源检查限定官网 HTTPS；下载流式检查 tar 内容和 gzip CRC，失败文件保留。
- 本节写入时 Quick Bilayer 实测尚在运行，新的完整 `build` 也已启动；**尚未宣称最终下载、GROMACS 编译或端到端验证成功**。最终验收记录应补充实际产物与检查报告。

HTTP 表单适配不是官方承诺稳定的 REST API。当前支持范围、配置字段、输入前提、恢复方式及验证层次见 [README.md](README.md)。

## 8. 盐参数验收发现文档与服务端不一致（2026-10-02）

旧 Quick Bilayer 测试任务请求的是 `Ion_type=KCl`、`Ion_conc=0.10`，但读取其服务端实际构建文件后发现不同条件（真实任务编号不公开）：

- [early-step4.3_ion.inp](runs/crtw-api-bilayer/early-step4.3_ion.inp) 引用了 `step2.2_ions_count.str`。
- [early-step2.2_ions_count.str](runs/crtw-api-bilayer/early-step2.2_ions_count.str) 明确写入 `pos = SOD`、`neg = CLA`、`conc = 0.15`，即服务端采用 NaCl 0.15 M，而非请求的 KCl 0.10 M。

这说明此次请求中，官网文档的大写开头盐字段未按请求生效。不能因提交成功、服务端回显参数、保留配体或将来 GROMACS 编译成功，就认定建模条件符合用户输入。两轮使用旧字段的测试任务均不作为参数验收通过的结果，原任务及证据文件保留，不删除。

修正为发送与官网链接的上游 Python/Bash 实现一致的小写 `ion_type`、`ion_conc`。第三轮完整建模已到达离子生成阶段；其 `early-step2.2_ions_count.str` 明确写入 `pos = POT`、`neg = CLA`、`conc = 0.1`、`niontypes = 1`。这提供了服务端采用 KCl 0.10 M 构建设置的直接证据，确认小写字段修复在该层面生效。

**已确认的是服务端生成设置，不是最终体系验收。** 最终归档下载、坐标/拓扑中的实际离子种类与数量、膜和配体完整性以及 GROMACS 编译仍待完成，尚不能宣称端到端通过。最新离线测试记录为 151 项全部通过；它们不替代真实产物验证。

验证命令现在可结合 `--build-config` 检查请求的脂质/水/离子种类，并要求服务端离子构建文件提供浓度直接证据。找不到浓度证据时应报告未验证；离子数/水数估算不能替代它。

## 9. 最终端到端验收（2026-10-02）

完整输入式 CLI 任务从前处理源任务进入 Quick Bilayer 建膜，真实任务编号不公开。输入配置为 [examples/crtw-build.yaml](examples/crtw-build.yaml)，蛋白 PDB 和配体 SDF 分别从 CrtW–β-carotene 结构拆分。`build` 自动完成本地检查/补氢、HTTP 上传、CGenFF 前处理与 API 建膜；恢复和下载没有重复提交远端任务，未使用浏览器。

最终产物：

- [标准归档](runs/crtw-full-build-corrected-ions/bilayer/charmm-gui.tgz)：324697405 字节、362 个 tar 成员；SHA-256 `6c145037bb1ecf3f37923966b2a1cc17135d9bccf6ebc87aa73f4631c1c8d833`。
- [完整验证报告](runs/crtw-corrected-validation-final/validation.json)：`passed: true`；[GROMACS 日志](runs/crtw-corrected-validation-final/grompp-fy6qwzyn/grompp.log) 和同目录 `system.tpr` 保留。
- [原始输入映射](runs/crtw-full-build-corrected-ions/inputs/input-manifest.json) 与 [运行记录](runs/crtw-full-build-corrected-ions/full-run.json)。

| 验收项目 | 实际证据 |
| --- | --- |
| 总原子数与拓扑顺序 | 坐标、拓扑均为 367489，逐原子名称/残基顺序一致 |
| 配体 | 一个 LIG，40 个碳重原子、56 个氢、97 个键；重原子名和完整重原子键连接图保留 |
| 脂膜和溶剂 | 942 POPC（尺寸流记录上叶 479、下叶 463），78620 TIP3 水 |
| 盐 | 143 POT、144 CLA、无 SOD；实际生成的单盐构建流明确 KCl 0.10 M |
| 盒子与水层参数 | `step3_size.str`：182.583126 × 182.583126 × 118.528735 Å；WBOXZ=22.5 Å。不据此反推 margin 算法 |
| 最终位姿 | 按 320 个蛋白 Cα 拟合：蛋白 RMSD 0.121908 Å，配体同一变换下 RMSD 0.307872 Å，最大位移 1.035349 Å |
| GROMACS 输入 | 2024.4，原始最小化 MDP，`-maxwarn 0`，退出 0，生成 TPR；0 warnings、1 NOTE |
| CGenFF 报告 | 最终归档 `lig/lig.rtf`：parameter penalty 1.400、charge penalty 1.442；低分不等于物理模型已充分验证 |

复核命令（输出需选新目录）：

```bash
charmm-gui-cli validate runs/crtw-full-build-corrected-ions/bilayer/charmm-gui.tgz \
  --input-manifest runs/crtw-full-build-corrected-ions/inputs/input-manifest.json \
  --build-config examples/crtw-build.yaml \
  --reference-pdb runs/crtw-full-build-corrected-ions/inputs/complex.pdb \
  --out runs/crtw-recheck --grompp --gmx /path/to/gromacs/bin/gmx
```

### 真实反例与兼容修复

旧任务的拓扑、配体位姿及 GROMACS 编译也通过，但实际为 215 SOD、216 CLA、0.15 M NaCl。针对请求的 KCl 0.10 M，最终反例报告正确得到 `passed: false`，失败集中于盐种类/浓度，而非因其他解析故障碰巧失败。

实际归档还暴露了两项仅靠模拟测试不易发现的兼容性问题，均已修复并加入回归测试：

1. API 在完整 gzip 后追加 ASCII 压缩字节长度；两份原始响应均通过 gzip CRC/长度校验。工具仅接受空尾部或恰好等于压缩长度的十进制尾部；规范归档与原始响应的 gzip 部分逐字节相同，原始响应保留。错误尾部、截断、坏 CRC、多 gzip 成员均拒绝。[规范化记录](runs/crtw-full-build-corrected-ions/bilayer/download-normalization.json)。
2. GROMACS 输出使用 HMR，氢模拟质量 3.024 Da，部分碳质量降低。验证不再把 `mass > 2.5` 直接当作元素判据，而优先使用 atomtype 原子序数，缺失时使用类型质量。实际拓扑和参数文件没有修改。

最终离线回归：174 项全部通过，包括 RDKit 补氢、严格归档处理、HMR 和相对位姿反例。早期报告保留供追溯；`early-validation.json` 的旧水计数、首次 `crtw-corrected-validation/validation.json` 的 HMR 误判均已被上述 final 报告取代，不应当作当前结论。

### 科学与兼容性边界

CHARMM 最终日志为正常终止，最严重警告为 level 1；短最小化到达设定步数且仍有 bent improper 警告，不能宣称充分收敛或已平衡。GROMACS 的唯一 NOTE 提醒质心移除与位置约束组合可能产生伪影；没有通过增加 `maxwarn` 绕过警告。本地只执行预处理，未运行 GROMACS 最小化或 MD。

归档生产 MDP 使用 4 fs、303.15 K 和 HMR；这些是该次服务器产物，不是本工具提供的通用物理保证。后续需按研究问题审查质子化、膜取向、脂质组成、配体参数和完整平衡流程。本次只证明这一 CrtW 单配体体系的 CLI 建模链路与所列文件检查通过，不证明任意蛋白/配体、共价配体、多配体或复杂金属中心已支持。所有报告继续保留 `scientific_correctness_verified: false`。

## 10. 0.4.0 验收与恢复更新（2026-10-02）

原始复合物直接输入的另一独立任务也已完成，最终归档 SHA-256 为 `93760128b1e53c51d2f482609db31932067504d8fd8622e4a0c9b6f0a01751ba`。相对蛋白拟合后的配体 RMSD 为 0.206126 Å；实际膜叶数量为上叶 479 POPC、下叶 463 POPC，KCl 0.10 M。GROMACS 输入编译与拓扑、环境、结合位姿检查通过。

新增的自动膜叶检查直接对照装填 HEAD 编号、最终 MEMB 残基和头基坐标，校验每叶实际数量及请求比例，不以盒子中心代替膜中面。当前明确支持磷脂 P 头基与 CHL1 O3；缺失证据或不支持的头基不能静默通过。CGenFF penalty 和 CHARMM 正常终止/最小化警告独立报告，两个真实模型均为 `passed_with_warnings`，不宣称充分最小化或平衡。

命名任务、分型恢复和批量调度复用同一提交保护及验收流程。GET 有限重试，POST 不自动重放；未知提交保留任务并要求核实。使用两个既有真实归档完成无 token 的批次中断恢复与 GROMACS 验收，没有再次创建远端任务。全新批量上传的多任务实测仍未覆盖；详细范围见 [验收记录](docs/TESTING.md)。

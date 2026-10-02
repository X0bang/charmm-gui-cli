# 高级配置、认证与恢复

常规使用只需 `login` 和 `build --protein ... --ligand ...`，见 [README](../README.md)。本页面向需要保留 YAML、官方 token 或已有 job ID 的用户。

## YAML 兼容输入

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
  upper: POPC=1
  lower: POPC=1
  margin_A: 20
  water_padding_A: 22.5
ions:
  type: KCl
  concentration_M: 0.10
```

```bash
charmm-gui-cli build system.yaml
```

路径相对于 YAML 所在目录。输出目录可省略，程序自动分配；默认等待、下载并验收。不要把 YAML 与直接结构/建模参数混用，否则会报错，避免某个参数被悄悄忽略。`--out`、`--no-wait` 和验证选项不属于这类冲突。

版本 1 的 `plan/run/resume` 只用于已完成 PDB Reader/CGenFF 的源任务，不能上传本地结构，不要与版本 2 的 `build/build-resume` 混淆。版本 1 的 `ligand_parameters_ready` 是用户声明，不是远端准备成功的证据。

## 官方 token 与自定义登录位置

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

`--token-file` 放在子命令前。凭据优先级为：显式文件 → `CHARMMGUI_TOKEN` 环境变量 → 全局会话 → 旧版当前目录 `session.token` → `~/.charmmgui_token`。选中的凭据失效不会静默换账户。环境变量 token 默认配全局 Cookie，用户必须确保同账户；无法确认时使用显式路径或默认 `login`。

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

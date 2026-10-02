# 完整性与验收记录

本页区分代码/安装验收、真实科学产物验收和仍在运行的任务；不以模拟测试替代实际建模结果。

## 0.3.0 项目完整性检查

| 范围 | 验证方式与当前结果 |
| --- | --- |
| 安装与发行 | 标准 wheel、源码 tar.gz、SHA256SUMS；白名单排除凭据、用户 PDB、runs、虚拟环境 |
| Ubuntu 22.04 | 实际 Ubuntu 22.04.3 / Python 3.10.12 容器，非 root 用户一键安装、全依赖、重复升级及 doctor/help/version 通过 |
| 跨目录登录 | 裸 login 同时保存两种会话；换工作目录访问同一真实 job 成功；旧 cwd token 不覆盖全局新登录 |
| 原始输入 | 独立 SDF/PDB、复合物自动拆分、明确原子名映射/唯一 SMILES 图映射；错误和歧义输入拒绝 |
| 自动任务管理 | 自动路径、最近任务恢复、提交防重、自动最终验证及证据绑定缓存 |
| 下载完整性 | gzip CRC/长度、有效 tar、精确字节数尾部兼容；截断/错误尾部/多 gzip 成员拒绝 |
| 静态验收 | GROMACS 拓扑/原子顺序/配体连接、HMR 元素识别、盐浓度直接证据、相对位姿 |
| 隐私与保留 | 密码不持久化；会话 0600；不删除旧安装/任务/失败产物；不分发私有测试结构 |

宿主当前全套：241 项测试全部通过，包含本地私有 CrtW 结构和 RDKit。
Ubuntu 最终代码源包测试：241 项测试，217 项通过、24 项明确跳过（不含用户私有结构等条件），退出码 0。包含任务状态和特殊 SMILES 化学语义回归。非 root 安装后，使用合成蛋白 PDB + 配体 SDF、不写 YAML、不带凭据，默认目录的 `build --dry-run` 和 `jobs` 均通过。安装和单元测试没有向远端创建模拟任务。

宿主已实际运行 `bash install.sh` 安装隔离版本，并从 `/tmp` 验证 `-h`、`doctor`、真实账户只读鉴权、原始复合物 `build --dry-run` 和旧归档 `build-resume --grompp`。最后一项返回 `state: validated`、`gromacs_compiled: true`；安装后的 Python 包源码与发布源代码逐文件一致。

```bash
python -m unittest discover -s tests -v
```

发行包不含私有 test-dataset，集成用例会明确 skip，而不是伪造通过；合成结构测试仍执行。无需账户或网络运行单元测试。

## 已完成的真实体系

从独立蛋白 PDB/配体 SDF 自动完成前处理及建膜，真实任务编号不公开。最终归档 SHA-256：

`6c145037bb1ecf3f37923966b2a1cc17135d9bccf6ebc87aa73f4631c1c8d833`

367489 原子，942 POPC，78620 水，143 POT、144 CLA，实际构建流为 KCl 0.10 M。配体 40 个碳重原子、56 氢、97 条含氢键及重原子图保留。最终配体在蛋白拟合后的 RMSD 为 0.307872 Å；GROMACS 2024.4 `grompp -maxwarn 0` 成功，0 WARNING、1 NOTE。

0.3.0 使用 `build-resume` 对这份原始归档重新执行自动验收（没有重新提交模型），得到 `state: validated`、`gromacs_compiled: true`。开发工作区证据位于 `runs/crtw-full-build-corrected-ions/results/validation.json`。该路径及原始模型不随公开源码包分发。

旧 NaCl 0.15 M 反例虽能通过 GROMACS 编译，却被 KCl 0.10 M 验收正确拒绝。详细实测、CGenFF penalty 和警告边界见 [RESEARCH.md](../RESEARCH.md)。

## 0.3.0 原始复合物新流程复测

已通过直接原始复合物命令进行本地准备，随后零路径 `build-resume` 自动完成真实上传和 CGenFF 参数化；真实任务编号不公开。开发目录 `runs/managed-crtw-0.3`。这不是复制旧任务结果，也未手工编辑自动生成配置。

**当前仍在远端建膜，尚未将这个新任务标为完成。** 旧真实模型的自动验收已经通过；新模型需要等最终归档再次执行全检查。

## 不能从通过结果推导的结论

GROMACS 编译不等于已执行 MD。CHARMM 服务端短最小化正常结束，但达到设定步数且仍有 bent improper 警告，不能称充分收敛。可解析警告涉及 POPC，部分大原子编号溢出不能可靠定位。体系仍需研究者审核质子化、取向、配体参数、HMR/时间步长及完整平衡过程。

Ubuntu 22.04 是实际测试平台；其他 Ubuntu 22+ 发行版的兼容依据是 Python 3.10+ 和隔离 venv，不能把它写成每个发行版/CPU 架构都已实测。未在容器内进行真实 CHARMM-GUI 建模或 GROMACS 科学验收。

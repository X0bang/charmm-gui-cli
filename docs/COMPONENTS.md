# 多配体与有机辅因子

```bash
charmm-gui-cli check --complex bound.pdb --components components.yaml
charmm-gui-cli build --complex bound.pdb --components components.yaml --name complex-a
```

配置见 [示例](../examples/components.yaml)。按 `resname`、`chain`、`resid` 精确选择实例；必要时增加 `icode`、`segment`。远端组名限定 1–3 个大写字母/数字、以字母开头，各组名必须不同。每组提供可靠 V2000 单分子 SDF，原子名称必须与 PDB 唯一对应，坐标以复合物为准，重原子不移动。SDF 缺少原子名映射、元素/电荷/图不匹配、未登记的 HETATM、跨组件 CONECT 或 LINK 都会阻断，不按距离猜键级、不做分子对接。

自动远端准备当前只允许不同残基名各一个有机、非共价组分，经各自 CGenFF SDF 字段参数化。程序先核验服务器实际表单提供每组控制项，再提交所有 SDF；若接口缺少字段明确停止，不虚构字段或默默只保留首组。完成证据要求每组 RTF/PRM；建膜 API 请求保留所有组名。最终逐组检查精确副本数、拓扑、键图和同一蛋白拟合下的位姿，再检查膜/水/离子与可选 grompp。

生成上传 PDB 时保留蛋白原有 TER/chain/segment 边界、SSBOND 和 CONECT，并在每个独立非共价组分前加入 TER；原子编号、名称和结合重原子坐标不因分隔改变。编号缺口不被当成断链。若官网仍把目标组分识别为蛋白工程残基，报 `component_classification` 并保留源任务及响应，不伪造参数化字段或自动重传；明确修正输入后创建新任务。

若官网重命名多组分重原子，只有同一任务可信归档中的规范 `step1_pdbreader.pdb` / PSF 能证明完整、唯一的原坐标及元素对应，并且映射后的最终重原子图、元素及副本数全部一致，才会用于位姿比较。PSF 的天然质量佐证元素，不按新名称猜元素或使用 HMR 后的原子质量。报告保留映射、文件哈希和归档归属；缺文件、多副本、坐标变化、歧义或图不符仍失败。不根据最终最小 RMSD 选择对应，不改变原始/最终文件，不放宽位姿阈值。

默认按化学定义补缺失氢；不预测 pH 或蛋白/辅因子的质子化状态。CGenFF penalty 按各自真实 RTF/PRM 审查，文件缺失或歧义标为 incomplete/review，而不是假定参数良好。

当前不支持金属配位、共价配体、重复同名副本的远端验收，也不支持多组分前处理复用。局部底层准备可显式保留水或非配位 Na/K/Cl，但 `check/build` 不会宣称这些实例已能被远端完整追踪。可信自定义 CHARMM RTF/PRM 可做本地化学映射检查，需 `--hydrogens preserve`；自动远端上传目前明确拒绝，因为未证明多套类型兼容与参数覆盖。

2026-10-11 已完成真实 4ZP0 膜蛋白 + DXC 底物 + LDA 去垢剂的端到端建模、同名中断恢复、最终逐组验收及 GROMACS 输入编译。LDA 的官网重命名通过上述归档证据严格核验。该单例不等于所有多配体/辅因子均已覆盖，多组分功能仍为实验性。

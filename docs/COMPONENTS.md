# 多配体与有机辅因子

```bash
charmm-gui-cli check --complex bound.pdb --components components.yaml
charmm-gui-cli build --complex bound.pdb --components components.yaml --name complex-a
```

配置见 [示例](../examples/components.yaml)。按 `resname`、`chain`、`resid` 精确选择实例；必要时增加 `icode`、`segment`。远端组名限定 1–3 个大写字母/数字、以字母开头，各组名必须不同。每组提供可靠 V2000 单分子 SDF，原子名称必须与 PDB 唯一对应，坐标以复合物为准，重原子不移动。SDF 缺少原子名映射、元素/电荷/图不匹配、未登记的 HETATM、跨组件 CONECT 或 LINK 都会阻断，不按距离猜键级、不做分子对接。

自动远端准备当前只允许不同残基名各一个有机、非共价组分，经各自 CGenFF SDF 字段参数化。程序先核验服务器实际表单提供每组控制项，再提交所有 SDF；若接口缺少字段明确停止，不虚构字段或默默只保留首组。完成证据要求每组 RTF/PRM；建膜 API 请求保留所有组名。最终逐组检查精确副本数、拓扑、键图和同一蛋白拟合下的位姿，再检查膜/水/离子与可选 grompp。

默认按化学定义补缺失氢；不预测 pH 或蛋白/辅因子的质子化状态。CGenFF penalty 按各自真实 RTF/PRM 审查，文件缺失或歧义标为 incomplete/review，而不是假定参数良好。

当前不支持金属配位、共价配体、重复同名副本的远端验收，也不支持多组分前处理复用。局部底层准备可显式保留水或非配位 Na/K/Cl，但 `check/build` 不会宣称这些实例已能被远端完整追踪。可信自定义 CHARMM RTF/PRM 可做本地化学映射检查，需 `--hydrogens preserve`；自动远端上传目前明确拒绝，因为未证明多套类型兼容与参数覆盖。

多组分输入、HTTP 编排及最终验收已有合成结构和模拟服务器测试，尚未完成真实多组分 CHARMM-GUI 端到端建模。已完成的真实单配体验收不能外推到所有配体/辅因子。

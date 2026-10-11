# 独立预检与坐标预览

## 终端摘要与离线结果报告

```bash
charmm-gui-cli jobs report receptor-a --format text
charmm-gui-cli jobs report receptor-a --format html --out result.html
```

默认保留 JSON 输出；`--full` 仅用于 JSON。HTML 是单文件报告，展示建模参数、体系组成、膜叶、配体位姿、编译状态与原始审查警告，已有 PNG 经坐标/归档/图片哈希核对后嵌入。没有有效预览时报告说明原因，不从其他任务借用图片；不会生成图片、上传或联网。查看 HTML 可以使用浏览器，但建模不需要浏览器。导出必须指定尚不存在的文件，原模型和报告不覆盖。报告可能含私有任务编号、路径及结构图片，分享前应检查；不自动上传 GitHub。

## 空间辅助检查

`check` 另外报告各配体的坐标范围、几何中心、最近蛋白重原子距离、4 Å 邻域接触和跨组分近距离原子对。2.0 Å 近距离与 0.8 Å 严重重叠阈值只是明确标注的启发式；不比较蛋白内部肽键或同一配体内部键，不依距离猜化学键，也不认证范德华冲突或膜取向。指标供人工复核，原有严格输入拒绝规则不变，不自动移动或修补结构。

```bash
charmm-gui-cli check --protein protein.pdb --ligand bound.sdf
charmm-gui-cli check --complex bound.pdb --ligand chemical.sdf --ligand-resname LIG
charmm-gui-cli check --complex bound.pdb --components components.yaml
charmm-gui-cli preview receptor-a
charmm-gui-cli preview structure.pdb --ligand LIG --out images --width 1400
```

`check` 不登录、不注册远端任务、不上传。默认输出简短摘要，`--full` 可显示完整化学图和逐残基记录。自动管理新的输出目录，也可用 `--out` 指定；`check.json` 保存输入 SHA256、化学定义、净形式电荷、缺失重原子和残基编号缺口，以及严格输入准备的结果。失败保留报告和部分文件，不覆盖已有目录。缺失原子/编号缺口要求复核，不自动修复，不仅凭编号断言有缺失残基，也不进行序列比对。预检通过不能证明质子化、力场质量或蛋白完整性。

默认生成 `side.png`、`top.png`、`ligand.png` 及坐标模型，蛋白无配体时不生成配体局部图。输入预览与最终体系预览分别标记来源；所有图来自实际原子坐标，不移动输出模型坐标。可重复指定 `--view side --view ligand`，宽度范围 400–4096。PNG 使用 NumPy/Pillow 的无显示服务投影，不需要桌面、GPU、Blender 或 PyMOL。

蛋白以 Cα 轨迹表示，配体/有机辅因子以重原子表示，脂质以头基和抽样尾部轨迹表示；水、离子和氢在图中隐藏，但模型不删这些原子。相机投影不是结构修改。局部图显示配体邻域的蛋白重原子与 Cα 轨迹；三视图只是预览，不据图片认证键级、膜取向、碰撞消除或平衡。

`build/build-resume` 验收后默认生成预览；`--no-preview` 可关闭。渲染依赖不可用或渲染失败不会把已完成的建模科学验收改为通过/失败，摘要独立显示预览错误。`preview NAME` 只从当前验收证据中定位模型，拒绝过期报告和与归档不同的提取坐标；缓存绑定坐标、验证指纹、工具版本、图像尺寸及图片哈希。可用新的 `--out` 另存，原预览和失败产物保留。

进度输出在 stderr，最终 JSON 在 stdout。进度区分前处理源 job、独立 Quick Bilayer job、下载、验收与结果；附官方结果链接。Quick Bilayer 的网页 Step 2 是结果页面，不等于普通 Bilayer Builder 的第 2 步；网页标签、API 内部步数和文件名前缀是不同编号。

不自动启动本地最小化、平衡、生产模拟或 Slurm 作业。归档内的引擎输入由研究者在自己的工作站/集群上审查运行；grompp 仅编译输入，不证明动态收敛。

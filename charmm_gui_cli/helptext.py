"""User-facing workflow guidance; no network access is needed to read help."""

OVERVIEW = """膜蛋白–配体脂膜建模 / CHARMM-GUI command-line workflow

第一次使用 / Quick start
  1. charmm-gui-cli doctor                  检查安装与可选 GROMACS
  2. charmm-gui-cli login                   登录一次，自动保存会话
  3. charmm-gui-cli build --protein protein.pdb --ligand bound.sdf
     自动准备输入、上传、建膜、下载并验证；无需手写 YAML。

已有结合好的复合物 / Bound complex
  charmm-gui-cli build --complex complex.pdb --ligand-resname LIG \\
    --accept-conect-bond-orders --upper POPC=1 --lower POPC=1 \\
    --salt KCl --salt-concentration 0.10
  上例必须有可信的 CONECT 键级；配体 PDB 不保证包含完整化学定义。
  缺少键级时提供带原子名映射的 SDF，或有连接图时用 --ligand-smiles。
  蛋白和配体须已有结合姿势；本工具不做 docking，不猜测键级或 pH。

任务与结果 / Jobs and results
  charmm-gui-cli jobs                       查看本地任务及结果位置
  charmm-gui-cli jobs status NAME           按任务名看阶段和恢复提示
  charmm-gui-cli jobs resume NAME           恢复同一任务
  charmm-gui-cli jobs report NAME           查看验收和需要审阅的内容
  charmm-gui-cli build-resume               继续最近任务，不重复提交
  charmm-gui-cli build -h                   输入组合、参数和更多示例
  中间文件自动保存在用户数据目录；--out 可选择自己的任务目录。
  默认等待并自动验证；--no-wait 提前返回，远端继续运行。
  验证通过不代表已完成充分最小化、平衡或生产模拟。

批量任务 / Batch
  charmm-gui-cli batch batch.yaml --dry-run 全员本地预检，不上传
  charmm-gui-cli batch batch.yaml --max-active 2
  charmm-gui-cli batch-resume BATCH_DIR     继续已有批次
  charmm-gui-cli batch-status BATCH_DIR     查看 JSON/CSV 汇总位置

高级兼容命令：plan/run/resume 用于已准备 job ID；web-* 用于诊断。
完整说明见发布包 README.md 与 docs/INSTALL.md。
"""

BUILD_GUIDE = """输入方式（选一种；无需管理拆分文件或自动生成的配置）
  --protein protein.pdb --ligand bound.sdf
      推荐：SDF 提供键级、电荷及同一结合坐标系中的配体。
  --name receptor-a
      给任务命名，之后 jobs status/resume/report receptor-a。
  --complex complex.pdb --ligand-resname LIG --accept-conect-bond-orders
      复合物内选一个配体，明确接受其 CONECT 键级。
  --protein protein.pdb --ligand ligand.pdb --accept-conect-bond-orders
      配体 PDB 必须含可信的 CONECT 键级；不会按距离猜化学结构。
  --complex complex.pdb --ligand chemical.sdf --ligand-resname LIG
      结合坐标取复合物，化学定义取 SDF；需唯一原子名对应。
  --protein protein.pdb --ligand ligand.pdb --ligand-smiles '...'
      PDB 仍需连接图，SMILES 定义键级/电荷；不接受歧义映射。

示例
  charmm-gui-cli build --protein protein.pdb --ligand bound.sdf \\
    --upper POPC:CHL1=3:1 --lower POPC:CHL1=3:1 \\
    --salt KCl --salt-concentration 0.15 --margin 20 --water-padding 22.5
  charmm-gui-cli build --complex complex.pdb --accept-conect-bond-orders --dry-run
  charmm-gui-cli build system.yaml --out runs/custom-build

默认模型参数是示例性设置，不是针对你的蛋白的科学推荐。
默认自动等待、下载和验证；安装了 gmx 时自动运行 grompp（不运行 MD）。
--dry-run 只在本地准备并检查，之后 login + build-resume 可继续。
SDF 必须 V2000 单分子；多配体、共价连接、复杂金属配位暂不支持。
"""

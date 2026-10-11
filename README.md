# charmm-gui-cli

通过命令行构建膜蛋白–配体脂膜体系，无需浏览器或手工管理中间文件。独立实验性客户端，非 CHARMM-GUI 官方软件。

## 安装

支持 Ubuntu 22.04+、Python 3.10+，需要 venv。安装器自动安装包括 RDKit 在内的依赖，不修改系统 Python。

```bash
git clone https://github.com/X0bang/charmm-gui-cli.git
cd charmm-gui-cli
bash install.sh
```

也可从 [download](https://github.com/X0bang/charmm-gui-cli/tree/main/download) 下载源码安装包，解压后运行 `bash install.sh`。若找不到命令，执行 `export PATH="$HOME/.local/bin:$PATH"`。

## 使用

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

主要输出为 `bilayer/charmm-gui.tgz` 和 `results/validation.json`。检测到 GROMACS 时自动执行输入编译检查，不运行 MD。

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

许可证：[GPL-3.0-only](LICENSE)。Copyright (c) 2026 X0bang.

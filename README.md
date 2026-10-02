# charmm-gui-cli

通过命令行构建膜蛋白–配体脂膜体系，无需浏览器或手工管理中间文件。独立实验性客户端，非 CHARMM-GUI 官方软件。

## 安装

支持 Ubuntu 22.04+、Python 3.10+，需要 venv。安装器自动安装包括 RDKit 在内的依赖，不修改系统 Python。

```bash
git clone https://github.com/X0bang/charmm-gui-cli.git
cd charmm-gui-cli
bash install.sh
```

也可下载源码安装包，解压后运行 `bash install.sh`。若找不到命令，执行 `export PATH="$HOME/.local/bin:$PATH"`。

## 使用

```bash
charmm-gui-cli doctor
charmm-gui-cli login

# 蛋白与配体必须处于同一结合姿势坐标系
charmm-gui-cli build \
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
charmm-gui-cli build-resume  # 继续最近任务
```

主要输出为 `bilayer/charmm-gui.tgz` 和 `results/validation.json`。检测到 GROMACS 时自动执行输入编译检查，不运行 MD。

## 注意

- 不做分子对接；普通 PDB 可能缺少键级，需要可靠 SDF 或符合要求的 SMILES 补充化学定义。
- 当前主要支持单配体、非共价复合物；不自动选择质子化状态或修补缺失片段。
- 验证通过不等于已充分最小化、平衡或适合生产模拟。
- 官网接口变化可能需要适配。源码包不含账户、会话、私有结构或历史模型。

[完整用法](docs/USAGE.md) · [安装说明](docs/INSTALL.md) · [高级配置](docs/ADVANCED.md) · [测试记录](docs/TESTING.md)

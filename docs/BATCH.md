# 批量膜蛋白–配体建模

批量功能复用单体系建模与验证流程，不执行分子对接，也不自动确定 pH。每个蛋白/配体必须已有一致的结合坐标；配体需要明确键级、电荷。默认模型设置不是针对任意蛋白的科学推荐。示例：[examples/batch.yaml](../examples/batch.yaml)。

## 清单与本地预检

```yaml
version: 1
defaults:
  upper: POPC=1
  lower: POPC=1
  salt: KCl
  salt_concentration: 0.10
jobs:
  - name: receptor-a
    protein: structures/a/protein.pdb
    ligand: structures/a/bound.sdf
  - name: receptor-b
    complex: structures/b/complex.pdb
    accept_conect_bond_orders: true
```

所有相对结构路径均以清单所在目录为基准。`defaults` 先应用，每个 `jobs` 项覆盖同名字段。名称必须唯一：1–64 个 ASCII 字母、数字、点、下划线或连字符，首字符必须为字母或数字。不接受未知字段、重复 YAML 键、重复名字、字符串冒充数值或布尔值。单项缺文件、结构化学检查失败会记录 `local_failed`，但不阻止其余项目完成本地预检。

支持直接建模字段：`protein`、`ligand`、`complex`、`ligand_resname`、`ligand_smiles`、`accept_conect_bond_orders`、`hydrogens`；参数：`upper`、`lower`、`salt`、`salt_concentration`、`margin`、`water_padding`、`orientation`、`n_terminal`、`c_terminal`。值与 `build -h` 相同；每项必须提供 `protein + ligand` 或 `complex`。不在清单中放密码、token、Cookie。

```bash
# 全员本地准备，不认证、不上传；指定目录必须不存在
charmm-gui-cli batch batch.yaml --out runs/batch-a --dry-run

# 登录后推进已有批次，默认整个远端生命周期最多一个活跃任务
charmm-gui-cli login
charmm-gui-cli batch-resume runs/batch-a

# 有限主动并发：最多两个活跃体系，启动间隔至少30秒
charmm-gui-cli batch-resume runs/batch-a --max-active 2 --submit-interval 30

# 单轮推进后返回；后续再恢复同一批次
charmm-gui-cli batch-resume runs/batch-a --no-wait
```

省略 `--dry-run` 的新批次也会先对全部项目做本地准备，再开始认证和上传。`--max-active` 默认 1，范围 1–4；活跃数量包含上传、配体参数化、建膜、下载和最终验证，不是 HTTP 请求线程数量。实际 HTTP 操作逐项执行，轮次间等待；`--submit-interval` 默认 30 秒并跨恢复保留，控制不同体系首次进入上传流程的间隔，不是同一体系内每个表单 POST 或克隆建膜请求之间的间隔。设为 0 仅取消本地节流，不代表服务允许高频请求；请遵守 CHARMM-GUI 服务使用规则。

## 恢复与失败

每个批次保存 `batch.json`，每个项目固定在 `jobs/<name>`。全局任务名额外带批次目录哈希前缀，避免不同批次同名冲突。恢复校验原始清单字节哈希和规范化配置；修改清单（包括注释）后不能原地恢复，请使用新批次目录。已准备任务不会重新创建，已记录的远端提交遵守单体系提交保护。进程锁防止两个调度器同时推进同一批次。

认证/会话失败暂停整批，重新 `login` 后恢复。提交结果不确定或未知远端状态会保留活跃名额并暂停，不能通过反复启动新批次绕过；需先检查原任务。已经确认的本地失败、参数化失败、远端错误或最终验证失败属于单项终态，其他项目继续。`complete` 表示队列结束，不表示全部科学验证通过；检查每项状态，任意失败返回非零退出码。

GET 临时网络重试耗尽会保留活跃名额，单轮模式返回 `waiting_network`；等待模式在本次时间上限内于后续轮次继续查询。服务端 `Retry-After` 指定的等待时间记录为 `next_retry_unix`，重启/恢复不会绕过等待。不确定的 POST 不会按临时 GET 重试处理。`--grompp` 会在远端推进前检查本地 GROMACS 是否存在。

若进程在本地准备期间被中断，恢复只创建尚未开始的项目；已有完整 `inputs_ready` 状态且全部准备文件存在时，采用原任务，不重复创建。半成品项目记录失败以待检查，不覆盖或删除后重建。

## 只读状态与汇总

```bash
charmm-gui-cli batch-status runs/batch-a
charmm-gui-cli batch-status runs/batch-a --csv batch-summary.csv
charmm-gui-cli batch-status runs/batch-a --remote-check
```

调度期间自动更新批次目录内的 `summary.json` 与 `summary.csv`，无需另行管理报告路径；汇总同时给出每项 `named_job`，可用于 `jobs show` / `jobs resume`。`batch-status` 默认仅读取本地文件并输出 JSON，可另导出 CSV，不更新自有报告。`--remote-check` 只对已记录的本人任务 ID 发 API GET，不推进步骤、不下载、不修改批次状态；源任务 API 的 `status: null` 不能视为失败或完成。CSV 做公式起始字符转义，且不包含凭据。每项最终归档与验证报告位于其任务目录，报告含环境和结合位姿检查；验证通过不替代能量最小化、平衡和科学审查。

批量调度已通过本地合成结构与模拟传输测试，并在同一批次中恢复、验收两个真实任务的既有归档，两项 GROMACS 编译均通过；没有额外向 CHARMM-GUI 提交新的批量任务。测试范围详见 [验收记录](TESTING.md)。

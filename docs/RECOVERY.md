# 命名任务与恢复

## 归档不需要的本地索引

```bash
charmm-gui-cli jobs forget receptor-a             # 只显示计划，不写文件
charmm-gui-cli jobs forget receptor-a --yes       # 确认后归档索引
charmm-gui-cli jobs relink receptor-a /path/to/receptor-a  # 恢复注册
```

`forget` 只将同一任务的名称、历史和最新记录移到各索引目录的 `forgotten/<操作编号>/`，保存原始字节、校验值和操作记录；不删除任务目录、模型、输入、日志、提交意图、会话或官网任务。它不会释放模型占用的磁盘空间。默认或 `--dry-run` 都只读，`--yes` 才执行。运行中的进程、未结束的建膜任务、结果不明或不可读取的提交意图会阻止操作。

`jobs relink` 可从未变动的索引备份中核对原任务身份并恢复名称注册，也支持任务整体移动后的新路径；同名已被另一个任务占用、备份被修改或任务身份不符时不会覆盖。原始任务仍可通过明确路径查看报告。保留的索引备份不是结构副本，不包含会话文件。

旧的无名称任务可用 `jobs forget /path/to/task --yes` 归档索引，再用 `jobs remember /path/to/task` 重新收录；`remember` 仅恢复本地索引，不调用建模流程。对有名称的任务，它按同样的备份/身份规则恢复注册，不绕过名称冲突。

## 建模与恢复命令

```bash
charmm-gui-cli build --name receptor-a --protein protein.pdb --ligand bound.sdf
charmm-gui-cli jobs
charmm-gui-cli jobs watch receptor-a
charmm-gui-cli jobs status receptor-a
charmm-gui-cli jobs status receptor-a --remote-check
charmm-gui-cli jobs resume receptor-a
charmm-gui-cli jobs report receptor-a
```

任务名为 1–128 个 ASCII 字母、数字、点、下划线或连字符，首字符为字母或数字。同名只能指向同一个任务；创建同名新任务会被拒绝并提示恢复原任务。目录路径和旧 `build-resume` 仍可用。名称索引保存在 XDG 数据目录，登录会话单独保存；换工作目录后可直接恢复。

## 移动任务目录

保留整个单目录任务，包括归档、`model/`、`inputs/` 和隐藏的 `.charmm-gui/`；不要在 worker 运行时移动目录。新任务的内部文件路径相对于任务根目录保存，状态查询时解析为当前位置。旧单目录记录中的绝对路径按原始嵌入位置兼容读取，不重写原始下载回执、输入哈希或验收凭据。

按名字找不到原目录时，程序会在当前目录及子目录按 `task_id` 查找；没有 UUID 的旧名称索引必须同时匹配原始名称、路径和注册时间，不靠名称猜测。查找不进入模型、输入、虚拟环境、隐藏目录或符号链接；达到查找上限或找到多个副本时，要求明确指定目录。只读查询不会修改索引。

```bash
charmm-gui-cli jobs status ./new-location/receptor-a
charmm-gui-cli jobs relink receptor-a /new/location/receptor-a
charmm-gui-cli jobs report receptor-a
charmm-gui-cli jobs export receptor-a --to ./exported
charmm-gui-cli --foreground jobs resume receptor-a
```

`relink` 只修改任务索引，不搬运模型或重发提交；拒绝身份不符、正在运行的任务，以及原目录仍存在的副本。前处理复用源也按 UUID 重新查找，继续核对原始快照、提交意图和输入哈希；任何证据变动仍会阻止复用。完成任务的本地恢复使用已有归档与模型，不需要再次建模。

`jobs status` 默认只读本地记录，显示阶段、错误分类、下一步和最终结果位置。`--remote-check` 仅读取已有建膜 job 的最新远端状态。远端拒绝时显示脱敏后的错误摘要，下一步指向同一任务的本地状态查询，不重复失败的远端查询。`jobs report` 按名称读取可信的最终报告，`--full` 输出完整 JSON。证据失效会列出缺失或改变的归档、输入、模型、参数或编译产物，再提示恢复同一任务重新检查。

## 错误与处理

| 分类 | 处理方式 |
| --- | --- |
| authentication | `login` 后恢复原任务；程序不保存密码或静默重登录 |
| access_denied | 确认登录账户拥有该任务，切换正确账户后恢复 |
| transient_network / transient_http | 建模期间 GET 默认退避等待最多 20 分钟；独立查询最多尝试三次，耗尽后记录可恢复错误 |
| transport_error | TLS/代理等非瞬时错误进入 `paused_network`，修复后显式恢复同一批次，不关闭证书验证或循环自动重试 |
| rate_limited | 遵守服务端 Retry-After；建模 GET 在等待窗口内分段等待，超过窗口则交回调度器，批次保存最早重试时间 |
| archive_preparing | 官网正在按需压缩归档；等待记录的重试时间后恢复原任务，仅重试下载，不重新建模 |
| component_classification | 官网把非共价组分识别成蛋白工程残基；保留原源任务，检查 TER 和分类，不伪造字段或重传旧意图 |
| wait_timeout | 远端任务继续存在，之后恢复原任务 |
| interrupted | 任务、阶段和错误历史保留，恢复同一任务 |
| remote_failure | 已确认的服务端失败，检查具体阶段及服务端产物 |
| submission_unknown | 写请求结果不确定，先核对既有任务，不自动重发 |
| archive_provenance | 归档与任务 ID/下载收据/已记录哈希不符；保留文件并检查，不覆盖收据或自动重建 |

自动重试只适用于只读 GET。上传与建模 POST 始终保留提交意图；网络失败、认证响应或 Ctrl-C 都不能证明服务端没有创建任务。认证问题也可能同时伴随 `submission_unknown`，此时登录后仍需核对原任务。

`CHARMM_GUI_GET_RETRY_SECONDS` 可设置建模 GET 的等待窗口（0–1800 秒，默认 1200；0 使用原短重试）。单次读取超时保持不变，TLS 证书错误和 401/403 不进入长重试。等待每次不超过 30 秒，`run.log` 记录等待阶段和心跳，`jobs status` 显示等待时间；网络恢复后清除等待标记并继续原步骤。

给 `report.txt` 或 `README.txt` 添加的备注不作为验收证据；重新生成时，先保存为 `.user-edited`（重复备注使用编号备份）。若唯一的 `model/` 不见了，恢复从原始归档重新解压并验收；修改过的模型或输入配置不会被覆盖。

官网可能先报告 `done`，但尚未生成归档；第一次下载会触发压缩，状态变成 `compressing`。程序将其显示为 `archive_preparing`，保留原响应和任务 ID，记录至少 120 秒的下载重试间隔。`--wait` 在时限内等待；单轮运行则提示稍后恢复。压缩可能超过官网提示的 1–2 分钟，这不等于模型失败，也不等于已经下载或验收通过。批量任务仍占用活动槽位，不另建替代任务。其他未知文本、HTML 或损坏归档仍会拒绝。

若不确定的 Quick Bilayer 提交实际上已创建任务，关联其**新建膜** job ID：

```bash
charmm-gui-cli jobs attach receptor-a --jobid YOUR_EXISTING_BUILD_ID
charmm-gui-cli jobs resume receptor-a
```

关联前会只读验证该 ID 可访问，并拒绝把前处理源 ID 当作建膜 ID。这只恢复 Quick Bilayer 提交；初始上传、选择或参数化表单的未知结果仍需要检查保留的响应和意图，不能用此命令任意跳过前处理。

网页准备意图使用 `charmm-gui-cli jobs reconcile receptor-a` 仅 GET 核对。确认已进入后续步骤时保存快照，之后 `jobs resume`。原步骤仍在时，人工确认确实未提交后才加 `--confirm-not-submitted`；记录归档保留，不自动 POST。上传响应丢失 ID 时加 `--source-jobid ID` 显式关联人工核对过的任务。无法判断时按显示的官网链接核对，意图保持不变。

0.6.0 的恢复默认在后台运行；需要同步退出码时执行 `charmm-gui-cli --foreground jobs resume receptor-a`。陈旧锁在重新启动时复用，活跃操作系统锁阻止重复启动；锁文件和日志保留。

## 离线验收与证据

已有完整归档时，本地恢复和验证不需要可用会话。若进程在下载完成、父任务状态尚未同步时中断，程序依据已有建膜记录和归档完成同步，再检查产物。

验收包括坐标/拓扑一致性、配体图和相对位姿、盐种类与浓度、每叶脂质组成、CGenFF penalty 及 CHARMM 完成日志。短最小化警告和参数评分作为审阅项呈现，`review_required` 与检查通过状态分开记录；没有执行本地 MD。

每份归档保留不可变 `download-receipt.json`，绑定建膜 ID、SHA256 与官方 `charmm-gui-ID` 根目录；前处理源、完整流程和 bilayer 的任务谱系必须一致。已下载归档的哈希变化不得默默重算覆盖，也不能拿其他任务归档替代。旧下载只有原已记录哈希与任务根一致时才允许迁移收据；没有这两项证据的本地文件不能自动标为该任务的最终模型。

缓存绑定任务谱系、下载收据、归档、输入、配置、工具版本、GROMACS 可执行文件身份以及编译日志/TPR 内容。证据变化或产物缺失后重新验收；归属不符则明确停止，批次状态也使用同一规则。恢复事件、旧报告、失败下载和旧安装均保留。

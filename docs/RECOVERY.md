# 命名任务与恢复

```bash
charmm-gui-cli build --name receptor-a --protein protein.pdb --ligand bound.sdf
charmm-gui-cli jobs
charmm-gui-cli jobs status receptor-a
charmm-gui-cli jobs status receptor-a --remote-check
charmm-gui-cli jobs resume receptor-a
charmm-gui-cli jobs report receptor-a
```

任务名为 1–128 个 ASCII 字母、数字、点、下划线或连字符，首字符为字母或数字。同名只能指向同一个任务；创建同名新任务会被拒绝并提示恢复原任务。目录路径和旧 `build-resume` 仍可用。名称记录保存在全局用户配置目录，换工作目录后可直接恢复。

`jobs status` 默认只读本地记录，显示阶段、错误分类、下一步和最终结果位置。`--remote-check` 仅读取已有建膜 job 的最新远端状态；远端 `done` 不代表已完成本地下载和验证。`jobs report` 按名称读取可信的最终报告，`--full` 输出完整 JSON。证据过期时会要求恢复任务重新检查。

## 错误与处理

| 分类 | 处理方式 |
| --- | --- |
| authentication | `login` 后恢复原任务；程序不保存密码或静默重登录 |
| access_denied | 确认登录账户拥有该任务，切换正确账户后恢复 |
| transient_network / transient_http | GET 最多尝试三次；耗尽后记录可恢复错误 |
| rate_limited | 遵守服务端 Retry-After；长等待交回调度器，批次保存最早重试时间 |
| wait_timeout | 远端任务继续存在，之后恢复原任务 |
| interrupted | 任务、阶段和错误历史保留，恢复同一任务 |
| remote_failure | 已确认的服务端失败，检查具体阶段及服务端产物 |
| submission_unknown | 写请求结果不确定，先核对既有任务，不自动重发 |

自动重试只适用于只读 GET。上传与建模 POST 始终保留提交意图；网络失败、认证响应或 Ctrl-C 都不能证明服务端没有创建任务。认证问题也可能同时伴随 `submission_unknown`，此时登录后仍需核对原任务。

若不确定的 Quick Bilayer 提交实际上已创建任务，关联其**新建膜** job ID：

```bash
charmm-gui-cli jobs attach receptor-a --jobid YOUR_EXISTING_BUILD_ID
charmm-gui-cli jobs resume receptor-a
```

关联前会只读验证该 ID 可访问，并拒绝把前处理源 ID 当作建膜 ID。这只恢复 Quick Bilayer 提交；初始上传、选择或参数化表单的未知结果仍需要检查保留的响应和意图，不能用此命令任意跳过前处理。

## 离线验收与证据

已有完整归档时，本地恢复和验证不需要可用会话。若进程在下载完成、父任务状态尚未同步时中断，程序依据已有建膜记录和归档完成同步，再检查产物。

验收包括坐标/拓扑一致性、配体图和相对位姿、盐种类与浓度、每叶脂质组成、CGenFF penalty 及 CHARMM 完成日志。短最小化警告和参数评分作为审阅项呈现，`review_required` 与检查通过状态分开记录；没有执行本地 MD。

缓存绑定归档、输入、配置、工具版本、GROMACS 可执行文件身份以及编译日志/TPR 内容。证据变化或产物缺失后重新验收；批次状态也使用同一规则。恢复事件、旧报告、失败下载和旧安装均保留。

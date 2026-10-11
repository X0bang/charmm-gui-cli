# 参数扫描与前处理复用

参数扫描对同一结合结构生成多个膜组成、盐浓度和盒尺寸条件。它复用已证明完成的 PDB Reader/CGenFF 前处理；每个体系仍用 `clone_job=true` 创建独立建膜任务，有自己的归档与验证结果，不修改原源任务。

## 清单

```yaml
version: 1
inputs:
  protein: structures/protein.pdb
  ligand: structures/bound.sdf
  hydrogens: add_missing
  orientation: ppm
defaults:
  upper: POPC=1
  lower: POPC=1
  salt: KCl
  water_padding: 22.5
axes:
  salt_concentration: [0.10, 0.15]
  margin: [20, 25]
```

该清单生成 4 个条件，名称为 `scan-0001` 至 `scan-0004`。轴名称排序后按清单内值的次序做笛卡尔展开，因此重复运行展开结果稳定。相对路径基于清单所在目录。单次最多 256 个条件，拒绝重复轴值、未知字段、空轴、不合法数值。

允许变化的轴仅为 `upper`、`lower`、`salt`、`salt_concentration`、`margin`、`water_padding`。蛋白、配体、原子坐标、键级/电荷、氢策略、配体残基名、N/C 末端和取向设置都属于固定前处理身份，不得作为扫描轴。`inputs` 支持单体系直接结构输入字段，也支持 `complex` 及可靠化学定义选项；`defaults` 只接受上述膜/盐/尺寸字段。示例设置不构成科学参数推荐，本功能不执行对接、不预测 pH。

当前扫描/复用接口只支持单一配体化学定义；多组件或多个化学组的建模仍走独立 build 流程，不能只拿首组配体的指纹复用整套前处理。检测到多组件元数据时明确拒绝复用，而不是忽略其他组。

## CLI 工作流

```bash
# 无凭据、无远端请求地展开并准备全部输入
charmm-gui-cli sweep sweep.yaml --out runs/scan-a --dry-run

# 登录后推进原扫描；调度限制沿用 batch
charmm-gui-cli login
charmm-gui-cli sweep-resume runs/scan-a --max-active 2 --submit-interval 30

# 底层批次与每项结果
charmm-gui-cli sweep-status runs/scan-a
```

扫描目录保留 `sweep.json`、`expanded-batch.json` 和 `batch/`。默认先完成全部本地预检；同一前处理身份只选一个尚未失败的源任务，其他项等待其完成前处理，再绑定源 ID。限制覆盖整个远端生命周期，绑定共享源 ID 本身不代表该子体系已提交。已开始上传或已经有提交 intent 的子项永不重新绑定。

清单或保存的展开结果变更后拒绝原地恢复。恢复不覆盖、删除或重建已有任务，失败和中断记录保留。服务端任务失效/权限拒绝时不自动退回重新上传，避免隐式创建额外任务。

## 复用的证据门槛

首次上传前保存 `preparation-seal.json`：蛋白、配体、复合物实际文件的内容 SHA-256，配体原子/键级/形式电荷图指纹，以及明确的氢、末端、取向等设置。指纹不包含输出目录或含绝对路径的 input-manifest，以便不同子目录的同一结构匹配。

源任务必须同时满足：上传凭据记录对应当前源 ID 和固定输入路径；链选择和配体参数化 POST 记录与原始页面及配置完全一致且已有响应；完成页面包含同一任务的 PDB、PSF、配体 RTF/PRM 链接，并明确进入膜构建第 2 步。只有 job ID、API `status: null`、或者一般性的“完成”文本都不够。

子任务保存源证书和身份指纹；每次首次克隆前重新核对源证据和子任务内容。原源任务后来完成建膜不使证书失效，但修改结构、前处理设置、已保存的 POST 或完成页面会阻止复用。

旧任务若没有上传前封存证据，不能事后补签或仅凭当前文件声明可复用。可选 `source_run: /path/to/original-run` 只接受具有完整证据的本地源目录，不接受裸 job ID。既有无 seal 的真实建模任务仍可正常查看、下载和验证，只不能用于这一严格复用流程。

普通批量清单也可显式设置顶层 `reuse_preparation: true`；默认关闭。启用后按前处理身份分组，各组独立选源，不会把不同蛋白/配体混用。

## 模块集成约定

`expand(path)` 离线返回稳定的批量清单；`prepare(path, directory)` 全员本地准备并封存；`read(directory)` 核验原清单和展开文件后恢复。

`seal_preparation(run)` 必须在首次上传前、持有该 run 锁时调用；已有 seal 只校验不重签。`certify_source(run)` 只读核验源任务；`bind_source(child, source)` 只绑定未提交的 `inputs_ready` 子任务，写入 `full-run.json.reused_preparation`、`source_job_id` 和 `state=prepared_reused`，不伪造任何上传 HTML。

完整建模流程遇到 `reused_preparation` 时，应在任何 Web 操作之前调用 `reuse_api_config(child, manifest)`，然后复用现有 `workflow.start` 的 intent/克隆保护；已有 `bilayer/run.json` 则继续原任务，不能重新克隆。绑定失败时必须报错，不能退回 Web 上传。`validate_reuse(child, manifest)` 返回验证后的原源 ID，支持集成测试。

批量调度每轮在统计活跃数量之前调用 `bind_ready_sources(batch_directory, manifest)`，跳过其标记的 `reuse_waiting` 项。共享源绑定但没有自己的 bilayer 提交记录的子任务不占远端活跃名额；实际开始克隆后才占槽。

本功能的证据判断与调度有离线合成结构和模拟服务器记录测试。真实单配体双盐浓度扫描已完成：一个 Web 前处理源、两个独立 API 克隆，KCl 0.10 / 0.15 M 均通过最终坐标、位姿、膜叶、离子、日志和 GROMACS 编译验收。归档准备等待及跨进程恢复使用原任务，没有重复上传；具体结果和科学审查项见 [测试记录](TESTING.md)。通过不等于已平衡或适合生产模拟。

# 双语错误迁移清单

高频认证/API/CLI 错误，以及共享配置校验、输入文件查找、文件系统路径和部分任务记录错误已接入双语目录。build_command、full_build、managed、workflow 的异常调用已全部提供 key，并以 AST 回归防止遗漏动态包装；历史 JSON message 仍保留原文。旧调用不传 `key` 时仍保留原文。新增文案请使用 `ToolError(key=..., params=...)`，补齐中英文且保持占位符一致。

YAML 错误显示实际配置路径和修正后可执行的本地复查命令，不回显配置值或解析器原文。直接参数的基线检查明确使用默认建模参数，不冒充用户请求的自定义配置。已有 JSON message 尽量保持兼容，翻译和上下文通过新增 message_key/message_params/details 提供；不重写历史记录。

## 尚未迁移的静态错误调用

以下按当前规范子包路径列出：AST 中 ToolError/AuthError/SubmissionUnknown 的第一个参数为静态字符串或格式化字符串，且未传入 key。动态包装调用、普通状态说明和图内标签不计入；调用数不等于独立英文文案数，也不代表每条都会在终端出现。行号随源码修改变化。

| 文件 | 调用数 | 行号 |
| --- | ---: | --- |
| [chem/input_sources.py](../charmm_gui_cli/chem/input_sources.py) | 27 | 48, 55, 68, 75, 78, 80, 82, 85, 88, 94, 96, 107, 110, 116, 130, 133, 135, 137, 141, 146, 194, 196, 207, 252, 257, 261, 308 |
| [chem/inputs.py](../charmm_gui_cli/chem/inputs.py) | 35 | 83, 103, 113, 120, 128, 132, 134, 141, 146, 168, 184, 187, 210, 219, 228, 230, 236, 250, 255, 276, 297, 303, 306, 330, 404, 411, 414, 494, 496, 520, 522, 524, 528, 539, 543 |
| [chem/multicomponent.py](../charmm_gui_cli/chem/multicomponent.py) | 58 | 31, 51, 54, 56, 58, 60, 64, 82, 85, 100, 105, 107, 110, 114, 117, 122, 126, 144, 146, 148, 173, 175, 185, 187, 193, 195, 203, 211, 219, 223, 228, 233, 243, 246, 255, 264, 347, 349, 353, 356, 359, 365, 373, 437, 439, 441, 444, 447, 452, 455, 483, 489, 496, 498, 515, 517, 525, 529 |
| [chem/pose_validation.py](../charmm_gui_cli/chem/pose_validation.py) | 25 | 23, 26, 34, 36, 46, 52, 55, 60, 66, 69, 81, 86, 92, 96, 103, 106, 114, 122, 148, 152, 154, 162, 165, 171, 175 |
| [chem/precheck.py](../charmm_gui_cli/chem/precheck.py) | 10 | 205, 207, 211, 213, 257, 267, 315, 317, 319, 391 |
| [chem/preview.py](../charmm_gui_cli/chem/preview.py) | 15 | 42, 56, 62, 67, 73, 101, 117, 127, 131, 141, 159, 236, 297, 299, 307 |
| [chem/structure.py](../charmm_gui_cli/chem/structure.py) | 3 | 24, 38, 47 |
| [chem/validation.py](../charmm_gui_cli/chem/validation.py) | 46 | 58, 123, 127, 132, 134, 137, 168, 170, 172, 189, 195, 198, 202, 209, 213, 236, 261, 272, 274, 277, 280, 289, 315, 319, 327, 329, 331, 350, 357, 358, 381, 384, 416, 427, 564, 649, 701, 707, 712, 719, 732, 734, 741, 749, 756, 764 |
| [commands/preview.py](../charmm_gui_cli/commands/preview.py) | 1 | 17 |
| [pipeline/acceptance.py](../charmm_gui_cli/pipeline/acceptance.py) | 15 | 38, 59, 66, 70, 83, 87, 91, 104, 110, 117, 120, 191, 195, 197, 230 |
| [pipeline/artifacts.py](../charmm_gui_cli/pipeline/artifacts.py) | 4 | 19, 31, 38, 53 |
| [pipeline/batch.py](../charmm_gui_cli/pipeline/batch.py) | 16 | 79, 89, 94, 129, 132, 141, 148, 150, 160, 211, 238, 249, 346, 656, 658, 661 |
| [pipeline/preview.py](../charmm_gui_cli/pipeline/preview.py) | 9 | 24, 27, 36, 43, 47, 54, 60, 67, 73 |
| [pipeline/sweep.py](../charmm_gui_cli/pipeline/sweep.py) | 38 | 35, 43, 45, 55, 58, 61, 65, 70, 73, 77, 83, 85, 99, 117, 141, 146, 154, 163, 169, 173, 196, 212, 238, 244, 259, 263, 274, 277, 280, 285, 294, 302, 344, 352, 357, 361, 401, 413 |
| [presentation/reporting.py](../charmm_gui_cli/presentation/reporting.py) | 1 | 211 |
| [remote/archive_transport.py](../charmm_gui_cli/remote/archive_transport.py) | 7 | 44, 57, 73, 81, 83, 85, 89 |
| [remote/web.py](../charmm_gui_cli/remote/web.py) | 23 | 33, 42, 153, 158, 194, 239, 245, 247, 255, 265, 268, 275, 284, 289, 316, 334, 336, 338, 345, 368, 372, 382, 389 |
| [store/locking.py](../charmm_gui_cli/store/locking.py) | 1 | 19 |
| [store/run_manifest.py](../charmm_gui_cli/store/run_manifest.py) | 2 | 13, 15 |

合计：336 处静态/格式化调用。

## 继续完善

- chem/inputs、input_sources 和 multicomponent 中的分子解析、化学映射及组分配置错误。
- validation、pose_validation、acceptance 和 precheck 中的详细结构/科学审阅说明。
- batch、sweep、preview 及 HTTP/归档处理中的剩余错误与恢复文案。
- 未列入上述静态表的动态错误包装、旧记录、图内标签和普通状态字符串，仍需逐类核对，不能据表中数量为零就宣称完成双语。

错误 category、JSON key、提交状态和重试语义不翻译。不因文案迁移重新提交任务、改写会话或重新封印证据；submission_unknown 仍须只读核对，POST 不自动重发。

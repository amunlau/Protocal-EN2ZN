# 文档结构与术语实例

当需要论文/技术文档模式、不译术语、复杂表格、行内公式或双格式输出时读取。本文件定义 `scripts/document_model.py` 的可选扩展。脚本仅验证记录的一致性；它不识别 PDF、不证明源文解析正确、不判断翻译语义，也不自动去水印或还原排版。

旧项目没有这些字段时仍可读取。旧 `structure` 不含 `schema` 时仅沿用原有源译指纹比较，不能称为已通过下面的表格拓扑验证。不要为了使用新规则直接覆盖旧 inventory；通过修订映射保留已审译文与原始证据。

## 文档类型与处理范围

`policy.document_type` 可为 `protocol`、`technical`、`paper`。内容块/segment 可选同名字段覆盖段落类型；缺省沿用项目约定，旧项目按既有约定执行。这只区分规则路由，不自动改变要求语气或术语译法。

源 PDF 总页数达到 20 页时仍须先确认样本；短论文的内部版面验证不等于要求用户额外批准。输出格式、样本组合和批准版本由台账管理，不能仅通过更改这里的类型/样式跳过关口。

## 术语策略与明确实例

术语原有必填字段保持不变，新增以下可选字段：

| 字段 | 取值与含义 |
|---|---|
| `strategy` | `translate`（默认）、`retain`、`first_use` |
| `case_sensitive` | 布尔值；省略时，包含字母且字母全大写的英文按大小写匹配，其余默认不区分 |
| `aliases` | 不重复的非空源词形/别名数组，不再次包含 `en` |

混合大小写产品名、API 和变量应显式设 `case_sensitive: true`。自动判断不能代替概念识别。例如 `CAN` 与 `can`、同名厂商架构部件、不同精度/层级的性能指标不能凭词面合并。`sense/scope/source` 继续保存定义、来源、版本和适用范围；跨文档的匹配只作候选。

`retain` 术语也保留 `zh` 字段，可填写同一原词或用于术语表的中文释义；正文检查按原文字形保留，不要求插入该中文释义。

仅明确绑定触发术语强制检查：旧 `term_ids` 继续有效；新项目优先记录出现实例。词面命中用于候选发现，不能自动把同形异义认定为同一概念。

```json
{
  "source": "CAN can operate.",
  "term_occurrences": [
    {"start": 0, "end": 3, "term_id": "can-bus", "action": "retain"}
  ]
}
```

位置是 Python/Unicode 字符索引，半开区间 `[start,end)`，不是 UTF-8 字节、UTF-16 代码单元或 PDF 字形序号。实例按位置排序且不能重叠；优先绑定完整术语。原文片段必须匹配该术语 `en/aliases`，源文校正或分段变化后必须重建位置，不能复用过期偏移。

实例 `action` 指定该次出现的展示方式，优先于术语默认 `strategy`：

- `translate`：检查对应中文译名至少出现所绑定的次数。
- `retain`：检查该实例的原字形（含大小写）至少保留相应次数。
- `first_use`：两者都检查；实际首次位置由最终文档/分册顺序决定，不能按每批上下文重置。

`first_use` 的后续出现可标 `retain` 或按已批准约定标 `translate`。只有 `term_ids` 而没有明确实例的 `first_use` 会产生待核 finding，不能假定当前块就是第一次出现。脚本不自动寻找全书首次，也不证明中英片段在译文中的语义对应正确；逐块审校仍须核对。局部重复或省略造成的误报应有具体解释。

`term_pattern(term)` 返回用于候选发现的正则；`term_matches(unit,term)` 仅返回是否有明确绑定，并包含子 segment。`check_term_usage(unit,target,policy)` 只检查当前节点；台账逐 segment 调用，防止一个格子的译词替另一个格子通过检查。

## 表格 schema v1

新表格必须逐格保存 `segments`，含空格；每个物理合并格只对应一个 segment。行列从 0 开始，跨度为正整数。`cells` 按起点行、列排列，ID 与 segments 顺序完全一致。

```json
{
  "structure": {
    "schema": "table", "version": 1, "rows": 2, "columns": 2,
    "cells": [
      {"id": "head", "row": 0, "col": 0, "rowspan": 1, "colspan": 2,
       "style": {"align": "center", "vertical_align": "center", "background": "#D9EAF7", "bold": true}},
      {"id": "left", "row": 1, "col": 0, "rowspan": 1, "colspan": 1},
      {"id": "right", "row": 1, "col": 1, "rowspan": 1, "colspan": 1}
    ]
  },
  "source": "Metric\nLatency\n",
  "segments": [
    {"id": "head", "source": "Metric"},
    {"id": "left", "source": "Latency"},
    {"id": "right", "source": ""}
  ]
}
```

检查边界、ID 唯一、跨度、重叠和完整覆盖，避免把被合并位置重复翻译。原文总串仍须按 segments 用换行连接。验证以矩形面积与坐标压缩扫线检查覆盖，时间复杂度为 O(n log n)，不建立 `rows × columns` 像素/单元格数组。

`cell.style` 与 `style_meta` 支持：

- `align`: `left/center/right/justify`；`vertical_align`: `top/center/bottom`。
- `background/color`: `#RRGGBB`；`bold/italic`: 布尔值。
- `font_name`: 非空字体名；`font_size/width/height`: 正数，单位 point。
- `padding`: `top/right/bottom/left` 非负 point 值。
- `borders`: 四边各自可含非负 `width`、`color`、`style`（`none/single/double/dashed/dotted`）。
- `custom`: 保存其它 JSON 细节，如旋转文字、多级表头角色、列宽数组和跨页连接依据。其语义尚未由脚本验证，不意味着生成器支持。

空白、0、横线、N/A 必须保持原区别。颜色、加粗可能指示实验组或最佳结果；不得把表格统一居中/加粗当作忠实还原。PDF 中原编辑器属性可能缺失，推断的对齐/合并须保留页图证据。对角线表头、非矩形视觉组合、嵌套表不强塞进这个模型；另行保存结构和核验记录，不能声称 v1 已验证所有表格类型。

## 行内占位符

已核对的公式或其它不能交给语言模型改写的行内对象，可在翻译输入中使用不与正常文本冲突的 token，同时保存原始提取与映射。下面的 `source` 是实际被替代的内容，不是新的翻译。只有完成映射验证后才可生成这种 inventory；不能用 token 掩盖原文遗漏。

```json
{
  "source": "When ⟦F_A⟧ holds, use ⟦F_B⟧.",
  "inline_placeholders": [
    {"id": "f-a", "token": "⟦F_A⟧", "source": "x > 0"},
    {"id": "f-b", "token": "⟦F_B⟧", "source": "y = x²"}
  ]
}
```

token 必须存在于块的 source 中，彼此唯一且不相互包含；实际出现区间也不能交叠，包括同一 token 自身重叠出现，否则无法独立恢复各对象。原文区间重叠拒绝登记，译文区间重叠产生 `placeholder_overlap` 待核项。译文保留 token，输出时由映射恢复实际内容；不得把 token 当作交付正文。每个 token 的数量固定，缺省允许因中文语序改变位置；`constraints.reorder: false` 的 token 相对顺序必须保持。

旧库存恢复/重查时也检查原文实例交叠，并产生 `source_placeholder_overlap`。即使译文已把 token 分开、数量相同，也不能掩盖原始映射歧义；须按源修订流程重建清单和对象映射，保留旧证据，不能只改目标文本后宣称已修复。

成对样式/对象边界可用 `constraints: {"pair_id":"emphasis-a","role":"start"}` 与对应的 `end`；每对有唯一开始/结束定义，原文和译文均须正确配对、嵌套。语义范围仍需审校，数量相同不代表包围了正确内容。无需为普通文本全部创建占位符。

## 来源、样式和输出信息分层

三个命名空间均可选；只校验明确定义的字段，额外信息放 `custom`，不允许 NaN/Infinity。

- `source_meta`: `document_id/source_version` 字符串、`source_sha256/processing_sha256` 哈希、`regions/assets/custom`。
- `style_meta`: 上述常用样式；用于记录来源或批准的排版样式。局部加粗等应绑定语义片段/行内对象，不能按英文字符偏移直接复制到中文。
- `output_meta`: `role` 字符串、`caption_id`、`reference_ids` 数组、`custom`。实际成品哈希与生成/验收状态由台账另存。
- `dependencies`: 唯一 ID 数组，不能包含自身；台账负责全清单的目标存在性/范围检查。引用外部文献不应伪装成内部依赖 ID。

`regions` 每项有 `page`（1 起的 PDF 页序）、`bbox: [x0,y0,x1,y1]` 和 `page_size: [width,height]`。坐标统一为**页面左上角原点、point 单位**，矩形有正面积且不越页，页序属于块的 `pdf_pages`。旋转、裁剪、两页合扫等保留 `transform`（6 个有限数值）及 `custom` 中的坐标变换依据；验证器不自行推断变换或检验 OCR 定位正确性。

`assets` 每项为 `{id,path,sha256,media_type?,custom?}`。`path` 是项目内规范相对路径，用 `/` 分隔，拒绝绝对路径、盘符、`..`、反斜线及 Windows 非法/保留名称。这里只验证引用格式，**读取资产的工具还必须验证真实路径未经符号链接逃出项目，以及实际文件 SHA-256**。原文件、清洗副本、排除对象和输出文件不得共用角色不明的引用。

结果载荷无需重复来源/样式元数据；它们由源清单持有。若结果带了这些字段且内容与源清单不一致，产生 `metadata_changed` finding。新表格的 `structure` 继续由台账要求源译一致；排版器必须从同一份已审 segments 构建 Word/PDF，不能再翻译一次。

## 机器字符串与异常字符

旧 `protected` 字符串保持有效。需要记录准确来源位置及类型时，可在 unit 或子 segment 新增可选的 `machine_spans`，无需批量迁移旧清单：

```json
{
  "source": "Use --mode.",
  "machine_spans": [
    {"id": "mode-option", "start": 4, "end": 10, "kind": "cli_option"}
  ]
}
```

`start/end` 是该节点 source 中的 Unicode 字符半开区间，不是 UTF-8 字节或 UTF-16 下标；源文修订后须重新定位。`id` 唯一、区间有效且不重叠，不能含首尾空白、换行或 NUL；`kind` 为 `url/doi/email/path/cli_option/escape`。分类由执行者核实，验证器不证明地址有效、链接可访问或该片段确实应保留。只绑定已确认的完整机器串，不为逃过检查截取词面前缀。

绑定后，以原始大小写和字符核对该节点内这一完整字符串的全部出现次数；不是只保护被标注的一次，也不按源字符位置要求中文语序。类型边界用于避免把 `--portable` 当成 `--port`，或把延长的 URL 当成原 URL。计数变化产生 `machine_bound_changed`；结果中擅改 `machine_spans` 产生 `metadata_changed`。若同串不同出现确需不同处理，先按语义规划节点或专门适配记录，不能滥用绑定全局替换。

另有保守的自动候选发现：普通 ASCII `http/https/ftp` URL、常见 DOI、email、`--long-option` 和部分转义写法。只有疑似缺失、新增或改变才产生 `machine_candidate_changed`；完整保留不报。自动命中不是绑定，也不是确定错译。路径、短旗标和国际化 URL 等不保证发现；数学、示例地址、LaTeX、代码字符串与局部本地化链接尤其要判定语境。源 URL 因换行/断词被提取损坏时先修解析，不能自动拼接、补协议、改大小写或访问链接验证来代替源文核对。

`discover_machine_spans(text)` 返回 `{kind,start,end,text}` 候选，供检查/审阅使用，不写清单。`check_machine_content(unit,target)` 返回 finding；已由旧 `protected` 检查的同一字面串不重复报自动候选。父 `segments` 聚合串不再重复自动检查，由台账原有逐格递归完成；明确绑定仍按所属节点检查。

U+FFFD 替代字符或 U+0000/NUL 出现在源或译文，产生 `unicode_source_or_target_corrupt` 高风险候选，即使两侧数量相同也要核对实际字形/文本层；不能复制乱码后声称保真。零宽字符、Bidi 控制、软连字符、词连接符和 BOM 只在源译计数变化时产生 `unicode_control_candidate`，附码点、两侧数量和至多各 8 个位置。原有合理同数保留不一概判错，位置样例不是全文清单；仍须检查其语义与视觉位置。不得一键删除控制字符或使用 NFKC 改写数学符号。候选需在审校中记录“确有问题并修复”或“可接受及依据”，不能凭检查名称直接断言误译。

这些检查借鉴 [Translate Toolkit 的语言相关检查及误报说明](https://docs.translatehouse.org/projects/translate-toolkit/en/latest/commands/pofilter_tests.html)、[Weblate 的机器串/零宽字符检查](https://docs.weblate.org/en/latest/user/checks.html) 和 [Okapi 的源译模式及损坏字符检查](https://okapiframework.org/wiki/index.php/CheckMate)。本实现独立、仅用标准库，不导入这些工具，也不继承其准确率或完整格式支持。

## 接口与验收边界

`validate_unit_extensions(unit)`、`validate_policy_extensions(policy)`、`validate_term_bindings(unit,policy)` 成功返回 `None`，记录非法时抛 `ValueError`。前两者在原有基本字段验证之后调用；第三个在活动术语表已校验后检查绑定，包含子 segment。

`check_term_usage(unit,target,policy)`、`check_unit_extensions(unit,result)` 返回 `{kind,detail}` finding 数组，由台账分配稳定 ID 并记录处置。后者检查行内对象数量、受限相对顺序、成对嵌套、机器字符串、异常字符和结果擅改元数据。checks 仍须按子 segment 执行。

结构/哈希/数量检查仅能发现确定性差异。源文到译文的遗漏、论断强弱、数字属于哪个核心/芯片/板卡、合并是否识别正确、Logo/水印判断及渲染保真仍需源译和页图核验。格式通过不能作为翻译准确率。

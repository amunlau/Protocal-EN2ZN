# 持久化、恢复与命令

## 数据与职责

Python 3.10+，标准库 sqlite3 必须可用；初始化和 PDF 探测另需 pypdf。使用当前环境中的解释器，不依赖固定安装路径。文档生成另用环境中可用能力。以下 `python` 均代表实际可用的解释器；命令路径相对此 skill，PROJECT 改成用户的实际项目绝对路径。

执行时把 `scripts/...` 展开为实际技能目录下的绝对路径，并把每个文件路径作为独立参数；当前工作目录可能是用户项目，不能照抄相对脚本路径。Windows/POSIX 示例、环境检查、不同宿主安装和迁移见 [跨平台适配](platform-compatibility.md)。命令输出按 UTF-8 解码；JSON/JSONL 文件用 UTF-8 保存。

项目推荐位于源文件旁 `文件名-zh-CN/`。保留：

- `translation.sqlite3`：权威台账；内含源标识、不可静默改写的原文块、草稿/已审结果、版本历史、术语/表达约定、确认及事件。
- 数据库版本 4 保留 `review_history`、双格式产物及独立输出约定，新增用于翻译记忆候选的派生索引。打开版本 1/2/3 时先生成并校验 SQLite 备份，再事务升级；失败回滚，原文清单哈希、样本版本、批准、译文和历史不被重写。版本 1 仅归档仍存在的审校，已丢失历史不能补造。缺少输出约定的旧项目默认仅 DOCX；已有输出约定原样继承。新增 PDF 必须明确更新输出约定，旧单格式批准不自动覆盖新增格式。
- `source-cache/`：逐页文本、OCR/页图等缓存；`inventory.jsonl`、`coverage.json`：结构清单与原页覆盖核对。
- `samples/`：实际交给用户的 DOCX/PDF 样本组合及说明；`chapters/`、`deliverables/`：分章与最终产物；`qa/`：渲染页图、回抽记录和检查证据。
- `backups/`：数据库快照；`exports/`：可读中间数据，供 DOCX 生成和审校。

原文/译文与状态在同一 SQLite 事务中提交，使用 FULL 同步和回滚日志。支持程序崩溃后回滚未提交事务；恢复完整性依赖正常工作的文件系统与存储。不能承诺恢复尚未提交的生成内容、损坏硬盘或已删除文件。独立数据库快照不包含源 PDF 和样本文件；备份/迁移应同时保留整个项目及源文件。

不要手改数据库、删除 journal 或将 DOCX 当作唯一恢复源。一个项目只由一个协调者提交；数据库锁能防并发写入破坏，但不替代任务分工。重启只恢复任务，不会在电脑关机时自动运行。

长文档台账和缓存宜放在当前操作系统的本地文件系统。WSL 从 `/mnt/c` 访问 Windows 目录时，SQLite 的大量小读写可能明显变慢；使用 Linux 原生目录可减少这类开销。跨平台迁移仍须复制整个项目及证据，按 `resume --source` 校验同一源文件，不能只移动数据库。

## 初始化与逐页缓存

**水印/页眉/页脚识别在 init 前完成**：按 [专项规则](pdf-furniture.md) 使用带坐标的布局缓存、候选报告及审查决定生成正文候选。pdf_probe 的原始文本仍可能含装饰，不得直接导入。项目同时保存 layout-cache、furniture-report、furniture-decisions、body-cache 与 exclusions；它们和原始页图属于恢复与备份资料。

清单的每个块可加入 `source_span_ids` 和 `processing_sha256`，将清洗后的正文定位到原始对象及处理版本。把适用的清洗版本/核验结论也记录到 policy 的 preprocessing 字段，使样本确认覆盖处理约定。清洗规则改变后不能把旧原文快照静默改写，需按修订项目迁移并重审受影响内容。

```text
python scripts/pdf_probe.py SOURCE.pdf --output PROJECT/source-cache --metadata-only
python scripts/pdf_probe.py SOURCE.pdf --output PROJECT/source-cache --start 1 --end 100
```

重复第二条会校验并复用已缓存页；可继续其他页段。失败页标 error，下次重试；空文本标 needs_visual_or_ocr。每页独立临时写入后替换，manifest 记录源哈希；源变更拒绝复用。解析/OCR/目录建立过程中同样要按批保存，不等到全文处理完才写文件。

原始文本缓存同时绑定提取器与 pypdf 版本；旧缓存缺版本信息或版本变化时用新目录，保留旧提取供回查。损坏的单页缓存可重提，不借用其他版本缓存。不要把已有普通文件夹当作空缓存输出目录。

将 policy.example.json 复制到项目，填写实际版本、范围、文体、规范性定义（不适用时为空）、行文和已核实术语。模板文字不是实际证据。`policy` 只存语义/翻译约定；输出格式、列宽、字体和底色放独立输出配置，不能将 `outputs` 塞入 policy。构建、核对 inventory.jsonl，例如：

```json
{"id":"s4.2-p001","order":100,"section":"4.2","kind":"paragraph","pdf_pages":[25,26],"printed_pages":["11","12"],"source":"The receiver shall retain 2 bits in Ready.","protected":["Ready"],"term_ids":["receiver"]}
```

kind 可为 paragraph、heading、list、table、caption、figure-labels、note、footnote、code、formula、identifier、literal 等。literal 只用于原样保留的数值、符号或书目字段，不能用来跳过正文。`structure` 须只包含不译的结构指纹（行列、单元格 ID、跨度、图片/公式 ID、脚注绑定），不能包含待译文本；它本身不能证明单元格译文对应正确。语义同形异义仍需审校。

```text
python scripts/translation_project.py --project PROJECT init --source SOURCE.pdf --inventory PROJECT/inventory.jsonl --policy PROJECT/policy.json --outputs PROJECT/output-policy.json
```

双格式任务的 output-policy.json 至少为 `{"formats":["docx","pdf"]}`，可附 `styles`、`layout` 对象记录实际约定。未传 `--outputs` 的程序接口仍默认 DOCX 以兼容旧任务；本技能的新双格式任务必须显式传入配置。运行环境及工具版本记到 runtime/产物证据，不混入语义 policy。

工具从 PDF 实际计页，不能手工填低页数。默认 ≥20 页强制样本；短文档可加 `--require-sample`。没有关闭长文档关口的参数。init 拒绝覆盖任何已存在数据库；初始化被中断而留下不完整数据库时保留它，用新目录重建，不覆盖已有译文。

完整清单一次登记，但构建过程可逐章保存，初始化逐行校验入库，不将全文载入内存。本版本没有渐进追加清单接口；不得手改数据库追加内容绕过样本签名。登记后修正原文、扩大范围或换源版本时建立修订项目，核验原文哈希后复用未变译文，不能直接复用旧审校/确认。块和 segment 的 term_ids、term_occurrences 必须指向活动术语；显式 dependencies 必须指向本清单其他块。类型化表格、行内占位符、术语实例及版式证据按 [文档模型](document-model.md) 登记。

需要校验图片/公式等外部源资产时，在提交任何译文前登记可选清单：`assets --manifest PROJECT/assets.json`。清单结构为 `{"assets":[{"id":"fig1","path":"assets/fig1.png","sha256":"实际文件哈希"}]}`。路径为项目内规范相对路径；禁止越界及移植到 Windows 后不可用的名称。登记后源资产不可静默替换；变化走源修订项目。恢复、导出、产物和样本检查会核验已绑定资产；数据库快照本身不包含这些文件。

## 逐段逐格绑定

新建表格和图内标签块采用 `segments`。旧的仅有 source/target 项目仍可恢复；重新核对其单元格对应关系，不能把兼容读取当作逐格验证已通过。普通段落可选用此结构。正文中的位置型错误仍需语义审查。

每个 segment 有稳定 id、source，可附 kind、protected、term_ids；行列/跨度/脚注绑定放 unit.structure。空格 source 为 `""`，译文也为空；不要填成 0、破折号或“不适用”。按清单顺序用 `"\n"` 连接所有 segment.source，作为 unit.source。例如下列片段应放入一个有 id/order/section/pdf_pages/kind 的完整 unit：

```json
{"source":"Minimum\n10 ns\nMaximum\n20 ns","segments":[{"id":"r1c1","source":"Minimum"},{"id":"r1c2","source":"10 ns","kind":"literal","protected":["ns"]},{"id":"r2c1","source":"Maximum"},{"id":"r2c2","source":"20 ns","kind":"literal","protected":["ns"]}],"structure":{"schema":"table","version":1,"rows":2,"columns":2,"cells":[{"id":"r1c1","row":0,"col":0,"rowspan":1,"colspan":1},{"id":"r1c2","row":0,"col":1,"rowspan":1,"colspan":1},{"id":"r2c1","row":1,"col":0,"rowspan":1,"colspan":1},{"id":"r2c2","row":1,"col":1,"rowspan":1,"colspan":1}]}}
```

结果提供相同顺序、相同 ID 的 segments，每项只含 id/target；result.target 同样用换行连接。所有问题统一放 result.issues，可用 location 标明 segment ID，不能将关键问题藏在 segment.issues 等未处理字段。DOCX 从这些已审 segments 与 structure 生成，不另手抄未检查的表格文本。

```json
{"target":"最小值\n10 ns\n最大值\n20 ns","segments":[{"id":"r1c1","target":"最小值"},{"id":"r1c2","target":"10 ns"},{"id":"r2c1","target":"最大值"},{"id":"r2c2","target":"20 ns"}],"structure":{"schema":"table","version":1,"rows":2,"columns":2,"cells":[{"id":"r1c1","row":0,"col":0,"rowspan":1,"colspan":1},{"id":"r1c2","row":0,"col":1,"rowspan":1,"colspan":1},{"id":"r2c1","row":1,"col":0,"rowspan":1,"colspan":1},{"id":"r2c2","row":1,"col":1,"rowspan":1,"colspan":1}]},"issues":[]}
```

ID 缺失/新增/重复/乱序、聚合文本不一致、空格被填值直接拒绝提交。每格独立核对数字、保护串与术语，literal 字段另查逐字保持；findings 的 location 指向 segment ID。因此 10 与 20 互换即使全文数字相同也会产生定位检查项。source 内嵌式表格必须先完成可靠结构还原，不能用随意分行制造假单元格。

数字检查覆盖常见十六进制、正负号、指数和 Unicode 数字上下标，技术符号检查覆盖常见比较/集合/逻辑符号。这不是数学解析器：`x^n` 等表达式、单位、端序、标识符内数字和 Word 样式上下标应使用 protected 与原图/语义复核。合理符号文字化或字体形式改变可能误报，须逐项解释；不得为通过检查改坏原义。

## 样本与真实批准

```text
python scripts/translation_project.py --project PROJECT select-sample --ids s4.2-p001 s7.1-table01
python scripts/translation_project.py --project PROJECT next
```

选段按 long-document-workflow.md，不得把全文都标为样本来绕过关口。用下面的 commit/review 流程完成样本，按同一组已审译文生成真实 DOCX/PDF。先分别以 `role=sample` 登记产物，完成本页“产物与交付关口”的 render/content/visual 三阶段检查，再登记组合。文件解析成功不等于内容或版面正确。

```text
python scripts/translation_project.py --project PROJECT sample --artifact PROJECT/samples/sample-v1.docx --artifact PROJECT/samples/sample-v1.pdf
```

工具返回 `version`，此时交付样本并等待。用户明确同意后，保存实际证据文件：`user_message`（原话）、`message_ref`（可用的消息标识；没有时记录日期与明确的样本交付/回复定位）、`approved_at`（含时区的时间）。不得将示例、测试文本、任务启动请求当作批准。

```text
python scripts/translation_project.py --project PROJECT approve --version RETURNED_VERSION --evidence PROJECT/sample-approval.json
```

组合每种约定格式须恰有一个文件。签名绑定样本译文、源/清单/语义约定、输出约定和全部文件哈希。脚本校验证据字段和版本，无法鉴别回复是否真正来自人类或语义是否同意；真实性与许可判断由执行者负责。样本译文/文件/选段、重要语义或输出约定变化后旧确认失效；同一版本重复登记不丢失批准。旧项目的既有 DOCX 批准保留，不伪造新版本的渲染/回抽证据；一旦任务新增 PDF 或变更输出约定，必须制作并确认新样本组合。

样本登记、批准和继续非样本处理均会重新核对所选样本的当前审校有效性，不能仅凭旧 `reviewed` 状态继续。检查器新增未处置问题时先 `resume` 定位并补审；样本内部修复仍可继续。只补充有效审校处置、译文和产物未变时保留原版本批准，不要求用户重复确认相同文件。

## 内容块提交与审校

```text
python scripts/translation_project.py --project PROJECT next
python scripts/translation_project.py --project PROJECT start --id s4.2-p001
python scripts/translation_project.py --project PROJECT commit --id s4.2-p001 --file PROJECT/work/result.json
```

result 示例（只表示结构）：

```json
{"target":"接收端应在 Ready 中保留 2 位。","issues":[]}
```

可加 `structure`、上下文说明等。未决问题用 issues 数组，每项须 severity（critical/major/minor）、status（open/resolved）、detail。草稿提交不代表审校完成。commit 输出 findings；用 `show --id 块ID` 取得该块当前 result_sha256 与 policy_revision（next 返回顺序上的下一未完成块），依据实际审查生成 review 文件：

```json
{"result_sha256":"复制当前译文哈希","policy_revision":1,"semantic":"记录实际原文对照结果","chinese":"记录实际中文行文检查","structure":"记录实际编号/表格/引用检查","resolutions":{}}
```

对每个仍存在的 finding，以其 ID 为键填写误报或允许差异的具体理由；真实错误应先改译文再重跑，而不是在 resolutions 中写“已修复”却不改结果。critical/major open 和待核标记不能通过 review。

`policy_revision` 必须为当前正整数版本，不能用 JSON 布尔值或小数代替。旧审校记录若含无效版本或损坏的审校元数据，恢复时保留原记录与译文并转为 `needs_review`，补做有效审校后继续。

```text
python scripts/translation_project.py --project PROJECT review --id s4.2-p001 --file PROJECT/work/review.json
```

state 为 pending、in_progress、translated、needs_review、reviewed。改译保留译文与审校历史；重复提交完全相同结果不增加版本，也不丢失有效审校。next 优先返回已保存待审/进行中的块，再取未译块；各组内按原顺序，最终导出始终按原顺序。它返回邻接原文、已绑定相关术语和显式依赖（仅参考）。前后邻块各截取最多 4000 字符；依赖上下文最多 16 块、共 20000 字符，单项源文/已审译文各最多 4000 字符；截断及 omitted 会明示。已审依赖译文须通过当前局部有效性检查，损坏或未决内容不作为已审参考。缺失所需定义时按 ID 定向读取，不把截断当作完整上下文，也不递归展开全书。

### 翻译记忆候选

`next/show` 的 `translation_memory` 只提供可解释的参考，不自动填入、提交、审校或批准。检索使用正文哈希、内容类型和 plain/segments 模式的已审状态部分索引，不逐次扫描全库译文。每次最多检索 32 条、返回 5 条，候选源文和译文各最多 2000 字符、合计最多 12000 字符。单条待载入记录不超过 512 KiB，单次加载不超过 4 MiB，返回的 TM JSON 不超过 64 KiB；超限与截断会明示，可按 ID 定向检查，不把省略当作没有重复。明确出现数字、规范性动词或否定/条件词变化的近似候选也会列出差异；这不是任意语义相似搜索或准确率评分。

候选须当前为 reviewed，源/译文哈希与审校绑定有效，当前确定性检查均有处置，且没有任何 open issue（含 minor）。返回来源块/节/页、结果和审校哈希、审校与当前 policy revision、当前语义签名及检查器版本。新审校保存语义签名；旧审校兼容读取时标记 `legacy_semantic_binding`，不能伪称已获得新版人工审校。无关词条新增可保留有效候选，关联语义变更导致的待审块不参与检索。

样本关口未通过时仅检索已选样本；显式 `show` 非样本也不会给出翻译记忆候选。同源异译会提示可能的上下文差异或冲突，不能自动统一。大小写、空白、符号保持在精确键中；即使源文逐字相同，也要核对术语义项、首次展开、表格结构和上下文。

### 有界批提交

已完成一小批草稿时可用：

```text
python scripts/translation_project.py --project PROJECT batch-commit --file PROJECT/batch.json
```

文件为 `[{"id":"块ID","result":{"target":"实际译文","issues":[]}}, ...]`，最多 32 项、8 MiB UTF-8 JSON，ID 不重复。每批必须全为样本或全为非样本，混批直接拒绝；非样本批先完整核验有效批准。一次批操作完整核验源和已绑定资产、当前批快照，并共享一次样本强哈希检查；不以文件属性缓存替代样本文件/证据哈希。任何项失败整批回滚，已提交的其他批不受影响。相同结果重试仍保留有效旧审校。

批处理只减少重复 I/O，不代表并发翻译，也不跳过逐块审校。原样重试会复核旧审校；若当前检查新增未处置问题，只转为待审并保留译文版本及历史，不误报已完成。样本批修改样本内容后仍会使旧批准失效；不能借批操作处理样本外内容。仅对短暂 I/O/锁等待故障有限重试同一批；格式错误、版本不符、未决质量问题和未批准样本应先处理原因，不能循环重试到“通过”。重启后先 resume，再重放未确认提交的批。

## 术语与规则变更

```text
python scripts/translation_project.py --project PROJECT policy --file PROJECT/policy-v2.json --reason "填写此次修改的具体依据"
```

脚本比较词条，按 term_ids/实例绑定及保守词面候选定位影响块；不会自动判定词义或替换译文。翻译检查只将明确绑定当作确定用法，CAN/can 等未绑定词面不会直接触发强制译名检查。检查词形变化、别名、图表内用法是否漏匹配，必要时补回查。影响块有结果时降为 needs_review。

只新增未触及样本的新术语不会重复索要样本确认。修改既有术语、删除词条、修改行文或规范性规则属于重要变更，旧确认失效；修订样本应覆盖改变之处，可重新选择相应块。源版本或范围变化必须新建修订项目。

已由原文 term_ids 绑定的词条不能直接删除而留下悬空 ID。优先保留概念 ID 修改词条并重审；确实要移除绑定时随原文清单建立修订项目。没有显式绑定的词条删除仍会按原文匹配回查。

只改字体、列宽等输出约定时使用 `output-policy --file PROJECT/output-policy-v2.json --reason "实际原因"`。独立输出 revision 增加，现有译文及语义审校保持；原样本版式批准和旧产物变为陈旧。当前为保守的整份输出配置失效，尚不按单个格式或单页增量失效。需重新生成/验收相关交付，但不要求全篇重新翻译。真实语义变化不能伪装为输出配置变更。

## 产物与交付关口

产物与文本块分开记录，`sample` 只覆盖已选样本，`full` 覆盖整个登记范围。已审全部译文、成功 export、样本产物通过均不等于最终交付完成。

```text
python scripts/translation_project.py --project PROJECT artifact register --id sample-docx --role sample --file PROJECT/samples/sample-v1.docx
python scripts/translation_project.py --project PROJECT artifact check --id sample-docx --stage render --evidence PROJECT/qa/sample-docx-render.json
python scripts/translation_project.py --project PROJECT artifact check --id sample-docx --stage content --evidence PROJECT/qa/sample-docx-content.json
python scripts/translation_project.py --project PROJECT artifact check --id sample-docx --stage visual --evidence PROJECT/qa/sample-docx-visual.json
```

PDF 用独立产物 ID 同样操作；最终文件用 `--role full`。登记会解析 DOCX 包的正文 XML/关系或 PDF 页面，绑定文件哈希、当前范围内的已审译文签名、输出约定签名和版本。它不调用模型或生成/渲染器。full 登记前须全部块已审且样本关口有效。

三类证据共有字段：`reviewer`、`at`、`details`、`passed:true`、`artifact_sha256`（取登记返回的 sha256）、`content_signature`、`output_signature`。这些必须来自实际操作，不得将模板或测试记录当验收。

- **render**：另含 `renderer`（实际工具及版本）、`pages`（按 1 起算连续页序排列，每项 `page/path/sha256`）。每页对应真实 PNG，此阶段按需使用 Pillow 验证 PNG 结构与校验和；不能仅伪造文件头。PDF 页图数量须与实际 PDF 页数一致。DOCX 页数以实际渲染器结果为准。保留全部页图；缩略图索引不能替代页图。
- **content**：另含 `extraction:{path,sha256}`，指向从实际产物回抽的 UTF-8 JSONL。逐行 `{id,target,segments?}` 与范围内清单顺序对应；有单元格/标签 segments 时也须逐项给出相同 ID 与实际回抽文字。脚本逐块逐格对账，仅将 CRLF 视为 LF，不折叠其他空白、符号或 Unicode。禁止复制台账目标文本冒充回抽；排版换行需由提取适配器根据真实位置复原并留证，无法可靠映射时保持未通过。
- **visual**：render/content 均通过后执行。另含 `unresolved:[]`，及非空 `screening`、`detail_pages`、`furniture`、`tables_figures_formulas`、`search_copy`，分别记录全页筛查、实际放大页范围、水印/装饰、复杂结构、搜索/复制层检查。记录不能证明审查确实发生，执行者仍需看页图；不能用“通过”填满全部字段代替真实结论。

重新登记同一文件/签名不丢有效检查，字节一致搬移可保留检查；文件、内容或约定变化使旧检查失效。页图/回抽证明丢失或被改，status/resume 同样报告陈旧。重新登记可清除损坏证据的当前通过状态，再补做检查，历史事件保留。重做 render/content 会清除旧 visual，需要重新确认版面。不要覆盖唯一已审成果，使用新输出位置后登记。

最终还须登记源内容覆盖：

```text
python scripts/translation_project.py --project PROJECT coverage --evidence PROJECT/qa/coverage.json
python scripts/translation_project.py --project PROJECT status
```

coverage 证据包含 `source_sha256`、`inventory_sha256`、与 policy 完全相同的 `scope`、`reviewer/at/details`、`unresolved:[]`，以及覆盖源 PDF 全部页的 `pages`。每页为 `{page,unit_ids,disposition,details}`；`unit_ids` 必须与原文块 pdf_pages 逐一对应，`disposition` 取 in_scope/out_of_scope/blank/nontext。含登记块的页必须为 in_scope；无块页面需明确范围外、空白或纯非文字处置，details 中记录保留资产和排除对象的依据，不把未识别正文标成纯图。可附 `proof_files:[{path,sha256}]` 绑定覆盖图和排除清单。对同页不同区域的已译、原样保留、排除和待核情况，保留原始区域记录，并在每页 details/proof_files 中定位。当前脚本验证页与块关系及证据绑定，不自动证明区域语义覆盖。

`delivery_complete=true` 需要：所有登记块已审、源覆盖记录有效、适用样本批准有效、每种所需格式至少一份当前 `full` 产物的三阶段检查全部通过。它表示记录完整且一致，不是翻译准确率保证。旧版项目也不会因为迁移成功自动取得这一新状态。

## 关机或中断后

```text
python scripts/translation_project.py --project PROJECT resume
```

resume 全量核验源 PDF、已绑定资产、数据库完整性和原文块/译文哈希，恢复有效样本确认并将中断状态变回待译或待审。返回双格式产物各阶段及陈旧原因；已有草稿、已完成生成和有效检查保留，只补未完成部分。status、产物登记/检查、export 同样完整核验源、已绑定资产与快照；其余普通命令用源文件属性发现常规变化，每次重启仍必须 resume 恢复中断状态。单次状态汇总复用各范围的内容签名；resume/export 不重复扫描已在本事务核验的快照。

恢复与导出也重新运行当前确定性检查，并检查当前审校是否存在、是否绑定同一译文及是否仍有关键未决问题。resume 会将有新问题的块持久降为 needs_review，recheck 列出 ID；原文、译文、旧审校和批准证据保留。正式 export 发现问题会拒绝；若其事务回滚，先 resume 登记待审状态再修复。没有新增问题的块不重译、不重新索要样本确认。新问题涉及样本时先复核；实际改动样本译文/文件后按原规则重新确认。旧载荷不合新检查时仍可通过 show/next 取回原稿修正。

源文件只换了位置时：

```text
python scripts/translation_project.py --project PROJECT resume --source NEW_LOCATION.pdf
```

字节一致才接受新位置。源内容改变则拒绝混入旧项目。样本文件丢失或改变会阻止批量继续；先从备份找回原版本。仅新版样本路径改变时，先按原产物 ID 对新路径执行 artifact register，再对组合执行 sample；字节、证据和约定均一致则保留原批准。旧 legacy 单 DOCX 样本仅重登 sample 即可。实际内容/约定变化时交付修订样本重新确认。已提交译文仍保留。

同一项目可由不同模型工具顺序接续；无需转换数据库或重新登记有效批准。跨机器迁移及布局缓存源路径的更新按 [适配指南](platform-compatibility.md) 执行。仅改变执行工具不修改翻译 policy。

数据库损坏时保留原项目，将通过完整性检查的快照恢复到新目录的 translation.sqlite3，并带上原样本与附件，再 resume；不要覆盖唯一原库。每章完成、重要约定变更前、主动暂停或交付前执行 backup。

```text
python scripts/translation_project.py --project PROJECT backup
python scripts/translation_project.py --project PROJECT export
```

export 会完整核对源/资产哈希、原文/译文快照与当前审校，要求全部块 reviewed 和适用的样本批准有效；输出 units、语义 policy、独立 output-policy、样本与状态中间数据，不等于 DOCX/PDF 成品。`--draft` 仅放宽审校/批准关口，不放宽数据完整性。每次导出新目录，逐条写出 JSON 数组，最后写 receipt.json；没有 receipt 的目录是不完整导出，不能交付。SQLite 是恢复权威源，导出/排版中断可从已保存的块重建。

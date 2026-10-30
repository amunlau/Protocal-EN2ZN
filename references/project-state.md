# 持久化、恢复与命令

## 数据与职责

Python 3.10+，标准库 sqlite3 必须可用；初始化和 PDF 探测另需 pypdf。使用当前环境中的解释器，不依赖固定安装路径。文档生成另用环境中可用能力。以下 `python` 均代表实际可用的解释器；命令路径相对此 skill，PROJECT 改成用户的实际项目绝对路径。

执行时把 `scripts/...` 展开为实际技能目录下的绝对路径，并把每个文件路径作为独立参数；当前工作目录可能是用户项目，不能照抄相对脚本路径。Windows/POSIX 示例、环境检查、不同宿主安装和迁移见 [跨平台适配](platform-compatibility.md)。命令输出按 UTF-8 解码；JSON/JSONL 文件用 UTF-8 保存。

项目推荐位于源文件旁 `文件名-zh-CN/`。保留：

- `translation.sqlite3`：权威台账；内含源标识、不可静默改写的原文块、草稿/已审结果、版本历史、术语/表达约定、确认及事件。
- 数据库版本 2 使用独立 `review_history` 追加保存审校，改译和改约定只清除当前通过状态，不删除历史。打开版本 1 项目时在事务内升级并归档仍存在的审校；更早版本已经丢失的审校不能凭空恢复。升级不改样本版本或伪造确认，原源/样本仍须校验。
- `source-cache/`：逐页文本、OCR/页图等缓存；`inventory.jsonl`、`coverage.json`：结构清单与原页覆盖核对。
- `samples/`：实际交给用户的样本 DOCX 及说明；`chapters/`、`deliverables/`：分章与最终产物。
- `backups/`：数据库快照；`exports/`：可读中间数据，供 DOCX 生成和审校。

原文/译文与状态在同一 SQLite 事务中提交，使用 FULL 同步和回滚日志。支持程序崩溃后回滚未提交事务；恢复完整性依赖正常工作的文件系统与存储。不能承诺恢复尚未提交的生成内容、损坏硬盘或已删除文件。独立数据库快照不包含源 PDF 和样本文件；备份/迁移应同时保留整个项目及源文件。

不要手改数据库、删除 journal 或将 DOCX 当作唯一恢复源。一个项目只由一个协调者提交；数据库锁能防并发写入破坏，但不替代任务分工。重启只恢复任务，不会在电脑关机时自动运行。

## 初始化与逐页缓存

**水印/页眉/页脚识别在 init 前完成**：按 [专项规则](pdf-furniture.md) 使用带坐标的布局缓存、候选报告及审查决定生成正文候选。pdf_probe 的原始文本仍可能含装饰，不得直接导入。项目同时保存 layout-cache、furniture-report、furniture-decisions、body-cache 与 exclusions；它们和原始页图属于恢复与备份资料。

清单的每个块可加入 `source_span_ids` 和 `processing_sha256`，将清洗后的正文定位到原始对象及处理版本。把适用的清洗版本/核验结论也记录到 policy 的 preprocessing 字段，使样本确认覆盖处理约定。清洗规则改变后不能把旧原文快照静默改写，需按修订项目迁移并重审受影响内容。

```text
python scripts/pdf_probe.py SOURCE.pdf --output PROJECT/source-cache --metadata-only
python scripts/pdf_probe.py SOURCE.pdf --output PROJECT/source-cache --start 1 --end 100
```

重复第二条会校验并复用已缓存页；可继续其他页段。失败页标 error，下次重试；空文本标 needs_visual_or_ocr。每页独立临时写入后替换，manifest 记录源哈希；源变更拒绝复用。解析/OCR/目录建立过程中同样要按批保存，不等到全文处理完才写文件。

原始文本缓存同时绑定提取器与 pypdf 版本；旧缓存缺版本信息或版本变化时用新目录，保留旧提取供回查。损坏的单页缓存可重提，不借用其他版本缓存。不要把已有普通文件夹当作空缓存输出目录。

将 policy.example.json 复制到项目，填写实际版本、范围、规范性定义、样式和已核实术语。模板文字不是实际证据。构建、核对 inventory.jsonl，例如：

```json
{"id":"s4.2-p001","order":100,"section":"4.2","kind":"paragraph","pdf_pages":[25,26],"printed_pages":["11","12"],"source":"The receiver shall retain 2 bits in Ready.","protected":["Ready"],"term_ids":["receiver"]}
```

kind 可为 paragraph、heading、list、table、caption、figure-labels、note、footnote、code、formula、identifier、literal 等。literal 只用于原样保留的数值、符号或书目字段，不能用来跳过正文。`structure` 须只包含不译的结构指纹（行列、单元格 ID、跨度、图片/公式 ID、脚注绑定），不能包含待译文本；它本身不能证明单元格译文对应正确。语义同形异义仍需审校。

```text
python scripts/translation_project.py --project PROJECT init --source SOURCE.pdf --inventory PROJECT/inventory.jsonl --policy PROJECT/policy.json
```

工具从 PDF 实际计页，不能手工填低页数。默认 ≥20 页强制样本；短文档可加 `--require-sample`。没有关闭长文档关口的参数。init 拒绝覆盖任何已存在数据库；初始化被中断而留下不完整数据库时保留它，用新目录重建，不覆盖已有译文。

完整清单一次登记，但构建过程可逐章保存，初始化逐行校验入库，不将全文载入内存。登记后修正原文、扩大范围或换源版本时建立修订项目，核验原文哈希后复用未变译文，不能直接复用旧审校/确认。块和 segment 的 term_ids 必须存在于活动术语表。

## 逐段逐格绑定

新建表格和图内标签块采用 `segments`。旧的仅有 source/target 项目仍可恢复；重新核对其单元格对应关系，不能把兼容读取当作逐格验证已通过。普通段落可选用此结构。正文中的位置型错误仍需语义审查。

每个 segment 有稳定 id、source，可附 kind、protected、term_ids；行列/跨度/脚注绑定放 unit.structure。空格 source 为 `""`，译文也为空；不要填成 0、破折号或“不适用”。按清单顺序用 `"\n"` 连接所有 segment.source，作为 unit.source。例如下列片段应放入一个有 id/order/section/pdf_pages/kind 的完整 unit：

```json
{"source":"Minimum\n10 ns\nMaximum\n20 ns","segments":[{"id":"r1c1","source":"Minimum"},{"id":"r1c2","source":"10 ns","kind":"literal","protected":["ns"]},{"id":"r2c1","source":"Maximum"},{"id":"r2c2","source":"20 ns","kind":"literal","protected":["ns"]}],"structure":{"cells":["r1c1","r1c2","r2c1","r2c2"],"rows":2,"columns":2}}
```

结果提供相同顺序、相同 ID 的 segments，每项只含 id/target；result.target 同样用换行连接。所有问题统一放 result.issues，可用 location 标明 segment ID，不能将关键问题藏在 segment.issues 等未处理字段。DOCX 从这些已审 segments 与 structure 生成，不另手抄未检查的表格文本。

```json
{"target":"最小值\n10 ns\n最大值\n20 ns","segments":[{"id":"r1c1","target":"最小值"},{"id":"r1c2","target":"10 ns"},{"id":"r2c1","target":"最大值"},{"id":"r2c2","target":"20 ns"}],"structure":{"cells":["r1c1","r1c2","r2c1","r2c2"],"rows":2,"columns":2},"issues":[]}
```

ID 缺失/新增/重复/乱序、聚合文本不一致、空格被填值直接拒绝提交。每格独立核对数字、保护串与术语，literal 字段另查逐字保持；findings 的 location 指向 segment ID。因此 10 与 20 互换即使全文数字相同也会产生定位检查项。source 内嵌式表格必须先完成可靠结构还原，不能用随意分行制造假单元格。

数字检查覆盖常见十六进制、正负号、指数和 Unicode 数字上下标，技术符号检查覆盖常见比较/集合/逻辑符号。这不是数学解析器：`x^n` 等表达式、单位、端序、标识符内数字和 Word 样式上下标应使用 protected 与原图/语义复核。合理符号文字化或字体形式改变可能误报，须逐项解释；不得为通过检查改坏原义。

## 样本与真实批准

```text
python scripts/translation_project.py --project PROJECT select-sample --ids s4.2-p001 s7.1-table01
python scripts/translation_project.py --project PROJECT next
```

选段按 long-document-workflow.md，不得把全文都标为样本来绕过关口。用下面的 commit/review 流程完成样本，再生成并查看真实样本 DOCX。台账无法验证 DOCX 内容与所选块是否语义一致，必须由执行者核对。

```text
python scripts/translation_project.py --project PROJECT sample --artifact PROJECT/samples/sample-v1.docx
```

工具返回 `version`，此时交付样本并等待。用户明确同意后，保存实际证据文件：`user_message`（原话）、`message_ref`（可用的消息标识；没有时记录日期与明确的样本交付/回复定位）、`approved_at`（含时区的时间）。不得将示例、测试文本、任务启动请求当作批准。

```text
python scripts/translation_project.py --project PROJECT approve --version RETURNED_VERSION --evidence PROJECT/sample-approval.json
```

脚本校验证据字段和版本，无法鉴别回复是否真正来自人类或语义是否同意；真实性与许可判断由执行者负责。样本译文/文件/选段或重要约定变化后旧确认失效；同一版本重复登记不丢失既有批准。

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

```text
python scripts/translation_project.py --project PROJECT review --id s4.2-p001 --file PROJECT/work/review.json
```

state 为 pending、in_progress、translated、needs_review、reviewed。改译保留译文与审校历史；重复提交完全相同结果不增加版本，也不丢失有效审校。next 优先返回已保存待审/进行中的块，再取未译块；各组内按原顺序，最终导出始终按原顺序。它同时返回邻接原文（仅参考）和相关术语，供继续处理。

## 术语与规则变更

```text
python scripts/translation_project.py --project PROJECT policy --file PROJECT/policy-v2.json --reason "填写此次修改的具体依据"
```

脚本比较词条，按 term_ids 和英文边界匹配保守定位候选影响块；不会自动判定词义，也不会自动替换译文。检查词形变化、别名、图表内用法是否漏匹配，必要时人工补回查。影响块有结果时降为 needs_review。

只新增未触及样本的新术语不会重复索要样本确认。修改既有术语、删除词条、修改行文或规范性规则属于重要变更，旧确认失效；修订样本应覆盖改变之处，可重新选择相应块。源版本或范围变化必须新建修订项目。

已由原文 term_ids 绑定的词条不能直接删除而留下悬空 ID。优先保留概念 ID 修改词条并重审；确实要移除绑定时随原文清单建立修订项目。没有显式绑定的词条删除仍会按原文匹配回查。

## 关机或中断后

```text
python scripts/translation_project.py --project PROJECT resume
```

resume 全量核验源 PDF 哈希、数据库完整性、原文块/译文哈希，恢复样本确认并将中断状态变回待译或待审。已有草稿保留，返回下一块。其他命令用文件属性发现常规变化；每次重启必须 resume 做完整哈希检查。

恢复与导出也重新运行当前确定性检查，并检查当前审校是否存在、是否绑定同一译文及是否仍有关键未决问题。resume 会将有新问题的块持久降为 needs_review，recheck 列出 ID；原文、译文、旧审校和批准证据保留。正式 export 发现问题会拒绝；若其事务回滚，先 resume 登记待审状态再修复。没有新增问题的块不重译、不重新索要样本确认。新问题涉及样本时先复核；实际改动样本译文/文件后按原规则重新确认。旧载荷不合新检查时仍可通过 show/next 取回原稿修正。

源文件只换了位置时：

```text
python scripts/translation_project.py --project PROJECT resume --source NEW_LOCATION.pdf
```

字节一致才接受新位置。源内容改变则拒绝混入旧项目。样本文件丢失或改变会阻止批量继续；先从备份找回原版本。仅样本路径改变时，对新位置重新执行 sample 登记；文件字节和约定签名均一致则保留原批准。实际内容/约定变化时交付修订样本重新确认。已提交译文仍保留。

同一项目可由不同模型工具顺序接续；无需转换数据库或重新登记有效批准。跨机器迁移及布局缓存源路径的更新按 [适配指南](platform-compatibility.md) 执行。仅改变执行工具不修改翻译 policy。

数据库损坏时保留原项目，将通过完整性检查的快照恢复到新目录的 translation.sqlite3，并带上原样本与附件，再 resume；不要覆盖唯一原库。每章完成、重要约定变更前、主动暂停或交付前执行 backup。

```text
python scripts/translation_project.py --project PROJECT backup
python scripts/translation_project.py --project PROJECT export
```

export 会完整核对源哈希、原文/译文快照与当前审校，要求全部块 reviewed 和适用的样本批准有效；仍需另行完成 PDF 覆盖与 DOCX 版面验收。`--draft` 仅放宽审校/批准关口，不放宽数据完整性。每次导出新目录，逐条写出合法 JSON 数组，最后写 receipt.json；没有 receipt 的目录是不完整导出，不能交付。SQLite 是恢复权威源，导出/排版中断可从已保存的块重建。

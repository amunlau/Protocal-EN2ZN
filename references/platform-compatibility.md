# 跨平台加载、环境与续译

仅在安装、首次运行或换工具时读本页。规则以 SKILL.md 和项目台账为准；模型能力与宿主工具能力分别确认。官方文档核对日期：2026-10-30。以下是加载方式核对，不代表已经在每个客户端完成真实协议翻译测试。

## 一份核心，多种入口

保持标准 `SKILL.md` 的 `name`、`description` 和相对资源链接，不依赖固定模型、厂商 SDK、MCP 服务名、专用询问工具或子代理。`agents/openai.yaml` 只是可选 OpenAI 界面信息；其他宿主不需要理解它。不要为兼容而在核心中加入 Claude 独有的动态命令、`context: fork`、`$ARGUMENTS`，或把某宿主的 `allowed-tools` 当作通用授权。

完整技能目录安装时命名为 **protocol-en2zh**，与 frontmatter 的 name 一致；保留 scripts、references、assets、agents 的相对位置，以及 README、requirements.txt、LICENSE 和 NOTICE。当前开发工作目录可以保留原名，但不能只复制 SKILL.md，或把名为 Protocal-EN2ZN 的目录直接当作符合标准的安装目录。分发时仅打包技能文件，排除 `.git`、环境、缓存和用户项目；安装后的入口为 `protocol-en2zh/SKILL.md`。开放格式的依据见 [Agent Skills 规范](https://agentskills.io/specification)。

| 宿主 | 项目级技能目录（后接 protocol-en2zh/） | 用户级目录（后接 protocol-en2zh/） | 调用或检查 |
| --- | --- | --- | --- |
| Codex CLI / IDE | `.agents/skills/` | `~/.agents/skills/` | `/skills` 检查；提示中用 `$protocol-en2zh` |
| Claude Code 本地会话 | `.claude/skills/` | `~/.claude/skills/` | `/protocol-en2zh`，或自然语言匹配 |
| Cursor Agent | `.agents/skills/` 或 `.cursor/skills/` | `~/.agents/skills/` 或 `~/.cursor/skills/` | 输入 `/` 搜索技能；也可自然语言匹配 |
| Gemini CLI | `.agents/skills/` 或 `.gemini/skills/` | `~/.agents/skills/` 或 `~/.gemini/skills/` | `/skills list` 检查；自然语言指定 protocol-en2zh |
| GitHub Copilot CLI | `.agents/skills/`、`.github/skills/` 或 `.claude/skills/` | `~/.agents/skills/` 或 `~/.copilot/skills/` | `/skills reload` 后 `/skills info protocol-en2zh`；自然语言指定 |

目录及调用依据：[Codex 官方指南](https://learn.chatgpt.com/docs/build-skills)、[Claude Code 官方指南](https://code.claude.com/docs/en/skills)、[Cursor 官方指南](https://cursor.com/docs/skills)、[Gemini CLI 官方指南](https://geminicli.com/docs/cli/skills/)、[Copilot CLI 官方指南](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills)。各平台的新旧版本、组织策略、云端会话可能不同；以实际技能列表和已载入路径核验。不能把上述 CLI 目录套用到同名产品的全部形态。

同机多工具可让支持者共用 `.agents/skills/protocol-en2zh/`，Claude Code 单独放置 `.claude/skills/protocol-en2zh/`。选择一个源码目录维护；同步副本时核对文件版本，避免同名旧副本抢占。不要同时把不同版本装在多个搜索位置。符号链接不是必需条件，Windows 权限和云端文件同步不一定支持。

Claude 网页端/Cowork 等采用宿主自己的技能上传或管理入口；不能假定会读取本机 `~/.claude/skills/`。详见上述 Claude 官方指南。其他支持 Agent Skills 的工具按其官方发现目录放置同一完整目录；不支持原生技能时可明确读取 SKILL.md 和必要引用作为指令，但必须仍有以下实际能力，才能执行完整工作流。

## 能力检查与有界降级

首次启动确认 skill 根目录、源文件、项目目录和实际 Python 解释器。目录中有空格或中文时必须作为完整参数传递；不要把用户给出的路径拼成未转义的 shell 代码。遵守宿主已有权限，不通过更换工具绕过限制。

`scripts/check_environment.py` 只读检查 Python 3.10+、SQLite、解析/生成依赖及候选工具。默认输出诊断；仅 `--require` 指定的阶段能力缺失才返回非零状态。基础阶段：`ledger` 台账恢复；`pdf-text` 初始化和文本探测；`pdf-layout` 内置文字层布局提取；`docx` 采用 python-docx 时的文档生成；`pdf-output` 为可选 PyMuPDF 原生输出的导入与必要 API 探测。重复 `--require` 可检查多个能力。版本、中文字体和实际支持的版式仍须另验；其他等效文档工具可用时无须强装同功能依赖。

产物 `render` 验收的 PNG 页图校验另需 Pillow，按需导入；当前 pdfplumber 的依赖已包含它。不能由 `ledger` 检查通过推断图像证据可验收，运行台账的解释器仍须能读取并验证实际 PNG。

| 必需能力或阶段 | 可用实现 | 缺失时的处理 |
| --- | --- | --- |
| 可读写、可保存的项目文件；执行 Python + SQLite | 宿主文件工具和终端/代码执行器 | 无持久存储时不启动长篇批量任务；先安排可下载、可恢复的完整项目。只读聊天可指导或翻译有限片段，不能伪称台账已保存 |
| PDF 计页/文本、文字层坐标 | pypdf、pdfplumber 或有等效记录的解析器 | 依赖按需准备；替代解析器必须保留来源、坐标和核验信息，不能把普通文本当成完成水印处理 |
| 页图渲染与实际查看 | 宿主 PDF/图像查看工具，或 Poppler、MuPDF 等生成页图后查看 | 不能看页图时暂存文字草稿和疑点；水印排除、复杂结构和最终版面保持待核 |
| OCR（仅需要的区域/页） | 宿主 OCR、OCRmyPDF、Tesseract 或已配置的布局/OCR 后端 | 查明语言包、坐标输出和置信不足处；不得只因命令存在就标为可用，不能用猜测补字 |
| DOCX 生成及渲染 | 可用文档技能/工具、python-docx；Word、LibreOffice 或其他验证过的渲染器 | 没有渲染器时不标版面验收完成，也不把 Markdown 改后缀冒充 DOCX；样本渲染不足时关口保持未完成 |
| 保源版式 PDF 生成、渲染及文字回抽 | 已验证的原生输出适配器或可回填审定译文的排版后端 | 需核验支持的区域/字体/样式、无法回填或溢出的处理。不能以 Word 预览 PDF 代替此能力；缺失时明列未交付格式 |

没有网络不妨碍使用已经具备的本地依赖与资料；把外部核验不足登记为待核。安装、下载、云 OCR 或外部翻译服务遵守现有用户授权及宿主要求；本技能不自带外部服务调用，也不要求配置 API 密钥。

环境检查不扫描整个磁盘，不安装全部可选工具；先用现有能力。执行结果如需留档，放在项目的 runtime 子目录，记录宿主/模型（只能记录实际已知值）、解释器、解析器及渲染器版本。运行环境记录不写入翻译 policy，避免纯粹换工具触发无关的样本重新确认。

可选后端与核心台账使用分离环境，接口交换稳定文件或明确版本的结构化数据。Docling、PaddleOCR、MinerU、GROBID、BabelDOC 等各有自己的 Python、系统、CPU/GPU、字体及模型要求；不因其中一个需要较新 Python 就把旧台账视为不可恢复。安装说明和实际版本可能不同，先查看版本/帮助再按所需阶段验证。固定成功验证的依赖与模型版本，升级保留旧缓存，重新验证受影响结构。

评估后端时使用本地代表页检查原生标签、混合文字/图、合并与彩底表、公式和多栏读序。验证输出能绑定源块、回填审定译文且失败可追踪；不能只测试可导入或CLI返回0。外部适配器的“自动继续/重新翻译”不得绕过本项目样本、审校和版本关口。第三方代码、模型和字体保留其自身许可证，本项目许可不能替代它们。

## 路径、编码与命令

以下路径是占位示例，执行时换成真实绝对路径。文档其他章节里的 `python scripts/...` 是简写：将脚本路径展开到已载入的 skill 根目录，将 SOURCE、PROJECT 等展开到实际数据位置；不要为此改变项目的工作目录。

Windows PowerShell：

```powershell
$skillRoot = 'D:\skills\protocol-en2zh'
$pythonExe = 'D:\tools\Python\python.exe'
$projectRoot = 'D:\协议资料\某规范-zh-CN'
& $pythonExe -X utf8 (Join-Path $skillRoot 'scripts/check_environment.py') --require ledger
& $pythonExe -X utf8 (Join-Path $skillRoot 'scripts/translation_project.py') --project $projectRoot resume
```

macOS / Linux / WSL 的 POSIX shell：

```sh
skill_root='/absolute/path/protocol-en2zh'
project_root='/absolute/path/某规范-zh-CN'
python3 -X utf8 "$skill_root/scripts/check_environment.py" --require ledger
python3 -X utf8 "$skill_root/scripts/translation_project.py" --project "$project_root" resume
```

`python`、`python3`、`py -3` 取决于实际安装；先核对版本，选定后始终用同一解释器检查和运行。`-X utf8` 是解释器参数，放在脚本路径前。内置命令输出 UTF-8 JSON；调用方按 UTF-8 解码。写入 JSON/JSONL 使用 UTF-8，不以 PowerShell 旧版默认重定向生成 UTF-16 文件。

Linux / Windows 的虚拟环境创建与依赖安装见 [README](../README.md)，环境放在技能目录之外。项目以 `.gitattributes` 统一源码 LF 换行；脚本显式交给 Python 执行，不依赖可执行位。资源引用按真实文件名大小写书写，以兼容 Linux 的大小写敏感文件系统。中文字体、OCR 语言包和渲染器分别配置，不依赖某台电脑的字体路径。

样本及登记输出在台账中使用 `/` 分隔的项目相对路径；注册时拒绝 `..`、绝对路径、Windows 保留名称、非法字符和末尾点/空格。保留中文和普通空格。已有样本仅改名或移动而各格式字节与确认约定完全一致时，可在新路径重新登记，原批准保留；字节或约定变化按 [项目状态](project-state.md) 处理。

模型返回结果应按 [台账约定](project-state.md) 写成合法 JSON，再提交检查。Markdown 围栏、厂商工具调用外壳、解释文字不是载荷；不能以字符串替换方式“修复” JSON 而改动译文、反斜线或保护字符。失败先读取原文件与错误，再定点修正；不无限重试相同调用。

## 换工具、换机器和云会话

1. 原会话先提交当前草稿，执行 backup，停止写入；由一个协调者写同一项目。跨工具不要并发续译同一块。
2. 同机直接读取原项目。换机器/容器时，保留完整项目、源 PDF、所有格式样本与输出、图像、核验和附件；SQLite 单文件快照不包含外部成果。进程仍在写入时不能只复制数据库；先正常停止，或用 backup 的已校验快照与完整附件建立迁移副本。
3. 在新宿主检查依赖和路径，以 `resume --source 新源文件绝对路径` 指定同一字节内容的 PDF。路径分隔符变化不改变样本的项目相对路径；原始 PDF 哈希、样本哈希和批准记录继续核验。不得为了恢复修改哈希或重置确认。
4. 源文件位置变化后，若继续使用布局缓存，先对新源路径运行 `pdf_furniture.py extract`，使用原缓存且显式指定本次所需的页范围；它校验哈希/引擎并更新源位置，复用匹配页。`pdf_probe.py` 同理。解析器版本不同则保留旧缓存，在新目录提取；已有原文清单不能随新提取结果静默改变。
   当前文本探测适配器版本 4、布局适配器版本 3，布局同时绑定 pdfplumber/pdfminer.six 版本；旧版本缓存作为证据保留，使用新目录重建。标记为 `source_invalidated` 的缓存不能因源文件换回原位置或原字节而继续复用。缓存完整性校验和台账恢复各自独立，重新解析不自动覆盖已审原文/译文。
5. 读取有效语义/输出约定、样本状态和 `next` 返回的块及相关上下文。仅换模型/工具不重译已审块、不再索要有效样本确认；新模型须沿用已批准表达，发现真实问题则登记并按原规则修订。旧版本项目先按台账迁移流程备份，不自动将其原 DOCX 输出升级成双格式；增加格式是独立的输出约定变更。
6. 云端或临时执行环境结束前，把完整项目保存到用户可访问的持久位置。若无法保证保存，停在可保存的检查点并说明待续位置，不能承诺下次还能读取临时容器。聊天中的“继续翻译”或平台会话恢复不等于项目恢复。

样本确认使用用户真实文字回复即可。`message_ref` 可记录可访问的会话链接/消息 ID，或日期加足以定位的对话标识；无专用消息 API 时保留原回复和可追溯说明，不能伪造平台 ID。旧项目已保存的有效批准证据跨宿主继续使用；缺失、版本不符或实际未获确认时才等待用户。

## 平台验收边界

安装后核对技能确实被载入、资源可读、依赖诊断和一次小型流程。用合成项目验证：20 页未确认时阻断正文翻译；双输出项目缺一种样本不能放行；已确认项目换工具/路径后仍有效；含中文空格的路径和 Unicode 内容无损；异常后草稿及输出进度可恢复。视觉效果与真实译文质量需另行验收，不能从“平台支持 SKILL.md”推导出各模型翻译质量相同，也不能从核心脚本跨平台推导所有可选后端都已在两平台测试。

# protocol-en2zh

**v2.0.0 · 英文协议 / 标准、技术白皮书 / 报告、论文 PDF → 简体中文 Word 与保版式 PDF**

面向 Claude Code、Codex 等模型工具的 Agent Skill。把长篇技术文件翻译组织为有术语约定、样本确认、逐块审校和持久进度的工作流，可在 Linux 与 Windows 使用。

本项目提供技能指令与本地辅助脚本，由宿主模型完成翻译和语义审校。解析、输出与视觉核验均须按实际能力执行，不承诺任意 PDF 自动无损复刻；脚本不内置翻译 API，也不自动安装 OCR 或大型模型。2000 页能力指分批处理和恢复能力，不能把合成规模测试当作真实文档已经完成翻译。

## 主要能力

- **长篇处理**：按章节和语义分块，逐页缓存、按需读取上下文，避免一次载入全文。
- **三类文体**：协议保留规范强度，技术文档保留条件与比较口径，论文保留证据强弱、研究局限和引用关系。
- **术语取舍**：按概念 / 语境选择中文、保留原文或首次解释；保护缩略语、模型名、代码、数值及公式，不将所有英文强行汉化。
- **样本关口**：源 PDF 总页数 ≥20，即使只翻译部分章节，也先交付约定格式的代表性样本并等待真实用户确认；默认 DOCX 与保版式 PDF 双样本。不能靠重启或切换工具绕过。
- **结构与版式**：核验多栏/通栏阅读顺序、图内标签、跨页表格、合并单元格、对齐、底色与强调；优先利用原生标签和几何，仅对困难区域补 OCR。
- **水印与页面装饰**：用对象位置、跨页模板、字体、角度、结构标记和页图交叉核验；保护真正标题、脚注、表头、警告和正文同名词。
- **断点续译**：草稿、约定、批准和历史审校保存到项目台账；停机后可恢复，换模型工具不重置有效确认。
- **双格式交付**：同一份审定译文生成可编辑 DOCX 与尽量保留原版式的 PDF，分别验收；生成器的受限结构须明确报出，不以图片化 Word 或重译来掩盖缺口。
- **可追溯交付**：保留分章成果、术语表、问题记录和输出版本；重要未决问题阻止正式交付。

v2.0.0 将原协议翻译流程扩展为协议、技术文档和学术论文三类工作流，并统一双格式交付。提供已审译文的有界上下文检索、跨章节双向一致性报告、机器字符串与异常字符检查，以及事务式小批提交。保版式 PDF 使用共享资源的分段复制，并重建支持的链接、书签和页标签；不能表示的导航与文档结构会明确报出。提取缓存核对源文件在操作前后是否一致，失败页与待视觉/OCR 页分别列出。

这些改进借鉴成熟工具的可验证机制，不以 GitHub 星数、模型自评分或项目自报成绩作为翻译准确率证据。可选的大型 OCR、布局和评估模型仍按真实样本验证后使用，不加入默认依赖。原有 20 页样本关口、Linux/Windows 适配和中断续译规则保持有效。

## v2.0.0 与旧项目

技能版本和数据格式分别管理：本版台账为 schema 4，文档模型和输出计划继续使用各自的版本字段。升级技能不会直接改写用户译文；首次对旧项目运行 `resume` 时，脚本先备份并事务式迁移受支持的旧台账，再核验源文件、审校和确认记录。迁移失败保留原库，不通过删库重建来恢复。

已有输出约定原样继承；缺少输出约定的旧项目沿用 DOCX，只有新项目默认双格式。新增 PDF、改变语义约定或实际修改样本时，按对应规则补做确认。新增检查发现未处置问题会转为待审；没有新问题的已审译文不需要重译。完整迁移与缓存重建规则见 [持久化指南](references/project-state.md) 和 [跨平台指南](references/platform-compatibility.md)。

内置工具执行状态与确定性检查；翻译、语义审校和视觉验收仍由实际具备能力的宿主执行。本版已验证的台账规模与局部版式用例不能替代用户文档的验收。复杂结构、受限导航和大型输出按 [输出能力边界](references/quality-and-docx.md) 选择经过验证的适配器。

## 环境与安装

核心工具需要 Python **3.10+**（含 sqlite3）、宿主文件读写与命令执行能力，以及能够持续保存的项目目录。PDF 页面和两种输出的最终核验还需要实际渲染与查看能力。核心脚本兼容 Windows、Linux，不依赖固定用户路径；可选解析/排版后端按其 Python、操作系统与硬件要求单独检查。

把完整目录放在宿主支持的技能位置，**目录名使用 `protocol-en2zh`**，与 SKILL.md 中的 name 一致。开发仓库本身可以保留其他目录名。

| 工具 | 常用项目级位置 | 调用示例 |
| --- | --- | --- |
| Codex | `.agents/skills/protocol-en2zh/` | `$protocol-en2zh` |
| Claude Code | `.claude/skills/protocol-en2zh/` | `/protocol-en2zh` |
| Cursor | `.cursor/skills/protocol-en2zh/` 或 `.agents/skills/protocol-en2zh/` | `/` 搜索技能 |
| Gemini CLI | `.gemini/skills/protocol-en2zh/` 或 `.agents/skills/protocol-en2zh/` | 自然语言指定技能 |
| GitHub Copilot CLI | `.github/skills/protocol-en2zh/` 或 `.agents/skills/protocol-en2zh/` | 自然语言指定技能 |

具体用户级目录、官方依据、云端限制和跨工具迁移见 [适配指南](references/platform-compatibility.md)。模型有能力读懂指令，并不意味着它所在的网页聊天界面具有完整的本地执行和持久存储能力。

以下命令在技能目录执行，将 Python 环境建在用户数据目录，保持技能目录精简。使用已有合适环境时可跳过创建虚拟环境；无需激活脚本，也无需更改 PowerShell 执行策略。

### Windows PowerShell

```powershell
$skillRoot = (Get-Location).Path
$venvRoot = Join-Path $env:LOCALAPPDATA 'protocol-en2zh\venv'
py -3 -m venv $venvRoot
$pythonExe = Join-Path $venvRoot 'Scripts\python.exe'
& $pythonExe -m pip install -r (Join-Path $skillRoot 'requirements.txt')
& $pythonExe -X utf8 (Join-Path $skillRoot 'scripts/check_environment.py') --require ledger --require pdf-text --require pdf-layout --require docx
```

### Linux

```sh
skill_root="$PWD"
venv_root="${XDG_DATA_HOME:-$HOME/.local/share}/protocol-en2zh/venv"
python3 -m venv "$venv_root"
"$venv_root/bin/python" -m pip install -r "$skill_root/requirements.txt"
"$venv_root/bin/python" -X utf8 "$skill_root/scripts/check_environment.py" --require ledger --require pdf-text --require pdf-layout --require docx
```

Debian / Ubuntu 若提示缺少 ensurepip，需由环境维护者准备相应的 `python3-venv` 包，再创建虚拟环境。依赖写入虚拟环境，不需要以 root 身份运行翻译脚本。

`requirements.txt` 记录直接运行依赖，传递依赖仍由 pip 解析；可选后端不要求全部安装。更换解析器版本会影响缓存，需按适配指南处理。OCR、PDF 渲染/排版器、LibreOffice / Word 和中文字体按任务需要配置；环境检查列出工具位置不代表已验证复杂版式。Linux 与 Windows 都应检查中文字体及数学符号的缺失与替换。DOCX 无须在两种系统上得到完全相同的分页，但都须通过语义、结构和版面验收。

内置保版式 PDF 适配器另需可选的 `PyMuPDF==1.26.7` 与本地中文字体，可用 `check_environment.py --require pdf-output` 检查导入条件，再按 [输出指南](references/quality-and-docx.md) 用实际区域验证。它支持经过位置核验的原生文字回填；复杂公式、图内标签及不支持的样式仍需专门适配。内置输出器尚不提供流式分卷；2000 页级交付须使用适合文档规模的分章生成与合成方案，不能由台账规模测试推断输出内存上限。

## 开始翻译

向已加载技能的工具发送，例如：

> 使用 protocol-en2zh 翻译指定英文协议、白皮书或论文 PDF，输出可编辑中文 Word 和尽量保留原版式的中文 PDF。先查找已有进度；源文件达到 20 页时先交付双格式样本，待我确认后继续。判断术语和缩写是否保留，核验图表、水印与页眉页脚，并保存可恢复的项目。

默认项目放在源文件旁的 `文件名-zh-CN/`；若源文件位于技能目录内，选择技能目录外的用户输出位置。用户示例原件保持原位，运行数据、样本、台账及译文不得写入技能源码或提交到本仓库。`.gitignore` 已忽略常见文档和运行数据，但不能识别任意自定义目录中的机密内容；发布前仍应检查暂存文件。

恢复示例中的脚本路径与项目路径均须换成实际位置：

```text
python /absolute/path/protocol-en2zh/scripts/translation_project.py --project /absolute/path/项目 resume
```

Windows 的路径引用写法、源文件迁移和有效样本恢复见 [持久化与命令](references/project-state.md)。仅“继续翻译”的聊天记忆不能替代台账；电脑关机期间不会自动运行。一个项目只由一个协调者写入；跨平台迁移先停写并完整备份，避免把运行中的 SQLite 数据库放在跨系统共享或同步目录中同时写入。

## 必要文件

| 路径 | 用途 |
| --- | --- |
| [SKILL.md](SKILL.md) | 模型入口、必守约束和按需阅读路由 |
| [references/](references/) | 按需读取的文体、文档结构、异常处理、排版、恢复与兼容指南 |
| [scripts/](scripts/) | 环境检查、文本探测、非正文处理、结构检查、输出与翻译台账辅助工具 |
| [assets/policy.example.json](assets/policy.example.json) | 语义约定模板；使用前按文体填写，输出约定独立登记 |
| [agents/openai.yaml](agents/openai.yaml) | 可选 OpenAI 界面信息，不影响其他宿主 |
| [requirements.txt](requirements.txt) | 运行依赖 |
| [LICENSE](LICENSE)、[NOTICE](NOTICE) | 许可证及必须保留的署名通知 |
| [.gitignore](.gitignore)、[.gitattributes](.gitattributes) | 排除运行数据、统一跨平台文本换行 |

版本记录在 SKILL.md 的 metadata.version；技能版本与台账/文档结构版本分别管理。完整复制上述技能文件即可安装；分发时排除 `.git` 与用户示例文档，保留 LICENSE 和 NOTICE。环境、缓存、用户项目和发行包放在技能目录之外。

GitHub 发布采用 `v2.0.0` 标签。提交仅包含上表的技能与发布文件；用户示例、译文、台账、页图和本地工具配置不属于发布内容。此仓库按精简技能分发，开发测试与调研产物在源码目录之外保存；不附带云服务、模型权重或自动翻译服务。

## 许可证与引用

项目原有指令、文档、配置与脚本采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，标准原文见 [官方许可证](https://polyformproject.org/licenses/noncommercial/1.0.0)。它限定许可用途，并要求再分发时保留许可证及 Required Notice；修改后分发同样适用。商业用途须另行取得权利人授权，具体许可范围及非商业组织等条款以完整许可证为准。

分发原版或修改版时保留 [NOTICE](NOTICE) 中的署名行，明确引用 **protocol-en2zh**。推荐同时提供原始仓库地址、所用版本和修改说明。

这是带非商业限制的源码可用项目，不将其称为允许自由商用的开源许可。第三方依赖和链接资料保留各自许可证；本许可证不授予源协议的版权，也不主张用户译文的所有权。

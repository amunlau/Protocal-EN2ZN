# protocol-en2zh

**v1.0.0 · 英文协议 / 标准 PDF → 可审校的简体中文 DOCX**

面向 Claude Code、Codex 等模型工具的 Agent Skill。把长篇技术文件翻译组织为有术语约定、样本确认、逐块审校和持久进度的工作流，可在 Linux 与 Windows 使用。

本项目提供技能指令与本地辅助脚本，由宿主模型完成翻译和语义审校。脚本不内置翻译 API，也不自动完成 OCR、Word 排版或视觉验收。2000 页能力指分批处理和恢复能力，不能把合成规模测试当作真实协议已经完成翻译。

## 主要能力

- **长篇处理**：按章节和语义分块，逐页缓存、按需读取上下文，避免一次载入全文。
- **一致性与准确性**：按概念 / 语境管理术语，保持要求、建议、许可、条件、否定、数值和标识符的原意。
- **样本关口**：源 PDF 总页数 ≥20，即使只翻译部分章节，也先交付代表性样本并等待真实用户确认；不能靠重启或切换工具绕过。
- **水印与页面装饰**：用对象位置、跨页模板、字体、角度、结构标记和页图交叉核验；保护真正标题、脚注、表头、警告和正文同名词。
- **断点续译**：草稿、约定、批准和历史审校保存到项目台账；停机后可恢复，换模型工具不重置有效确认。
- **可追溯交付**：生成全文 / 分章 DOCX、术语表和问题记录；重要未决问题阻止正式交付。

## 环境与安装

需要 Python **3.10+**（含 sqlite3）、宿主文件读写与命令执行能力，以及能够持续保存的项目目录。PDF 页面和 DOCX 版面的最终核验还需要实际渲染与查看能力。原生支持 Windows、Linux；不依赖 Bash、PowerShell 特有语法或固定用户路径。

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

`requirements.txt` 固定本版验证过的直接依赖：pypdf、pdfplumber、python-docx；传递依赖仍由 pip 解析。更换解析器版本会影响缓存，需按适配指南处理。OCR、Poppler / MuPDF、LibreOffice / Word 和中文字体按任务需要配置；环境检查列出工具位置不代表已验证其实际效果。Linux 渲染需要可用的中文字体，Windows 也应检查字体缺失与替换。DOCX 无须在两种系统上得到完全相同的分页，但都须通过语义、结构和版面验收。

## 开始翻译

向已加载技能的工具发送，例如：

> 使用 protocol-en2zh 翻译指定英文协议 PDF，输出简体中文 DOCX。先查找已有进度；源文件达到 20 页时先交付样本，待我确认后继续。统一术语，识别水印与页眉页脚，并保存可恢复的项目。

默认项目放在源文件旁的 `文件名-zh-CN/`。原始 PDF、缓存、样本、台账及译文放在项目目录，不写入技能源码或提交到本仓库。`.gitignore` 已忽略常见文档和运行数据，但不能识别任意自定义目录中的机密内容；发布前仍应检查暂存文件。

恢复示例中的脚本路径与项目路径均须换成实际位置：

```text
python /absolute/path/protocol-en2zh/scripts/translation_project.py --project /absolute/path/项目 resume
```

Windows 的路径引用写法、源文件迁移和有效样本恢复见 [持久化与命令](references/project-state.md)。仅“继续翻译”的聊天记忆不能替代台账；电脑关机期间不会自动运行。一个项目只由一个协调者写入；跨平台迁移先停写并完整备份，避免把运行中的 SQLite 数据库放在跨系统共享或同步目录中同时写入。

## 必要文件

| 路径 | 用途 |
| --- | --- |
| [SKILL.md](SKILL.md) | 模型入口、必守约束和按需阅读路由 |
| [references/](references/) | 7 份翻译规范、异常处理、排版、恢复与兼容指南 |
| [scripts/](scripts/) | 4 个运行脚本：环境检查、文本探测、非正文处理、翻译台账 |
| [assets/policy.example.json](assets/policy.example.json) | 项目约定模板；使用前按源协议填写 |
| [agents/openai.yaml](agents/openai.yaml) | 可选 OpenAI 界面信息，不影响其他宿主 |
| [requirements.txt](requirements.txt) | 运行依赖 |
| [LICENSE](LICENSE)、[NOTICE](NOTICE) | 许可证及必须保留的署名通知 |
| [.gitignore](.gitignore)、[.gitattributes](.gitattributes) | 排除运行数据、统一跨平台文本换行 |

版本记录在 SKILL.md 的 metadata.version；当前为 1.0.0，数据库格式为 2，两者分别管理。完整复制上述技能文件即可安装；分发时排除 `.git`，保留 LICENSE 和 NOTICE。环境、缓存、用户项目和发行包放在技能目录之外。

## 许可证与引用

项目原有指令、文档、配置与脚本采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，标准原文见 [官方许可证](https://polyformproject.org/licenses/noncommercial/1.0.0)。它限定许可用途，并要求再分发时保留许可证及 Required Notice；修改后分发同样适用。商业用途须另行取得权利人授权，具体许可范围及非商业组织等条款以完整许可证为准。

分发原版或修改版时保留 [NOTICE](NOTICE) 中的署名行，明确引用 **protocol-en2zh**。推荐同时提供原始仓库地址、所用版本和修改说明。

这是带非商业限制的源码可用项目，不将其称为允许自由商用的开源许可。第三方依赖和链接资料保留各自许可证；本许可证不授予源协议的版权，也不主张用户译文的所有权。

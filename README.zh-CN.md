# GLPI Followup Translate

使用本地 [Ollama](https://ollama.ai/) LLM 自动翻译 GLPI 工单。检测中文或英文内容并进行双向翻译（中文 ↔ 英文）。支持工单**标题**、**描述**、**跟进记录**、**任务**、**解决方案**和**审批**。

> 📖 [English](README.md) | 简体中文

## 功能特性

- 🔄 **守护进程 / 单次执行** — 定时轮询或单次运行
- 🌐 **语言检测** — CJK 感知，支持中英混合内容
- 🔀 **双向翻译** — 中文 → 英文，英文 → 中文
- 📝 **保留原文** — 翻译追加到原文后，不覆盖原始内容
- 🎨 **富文本感知** — HTML 格式保留；冗余样式属性自动精简
- 📦 **完整时间线** — 跟进、任务、解决方案、审批（请求 & 回复）
- 🚫 **去重保护** — 内容哈希 + 内嵌标记，双重防重复
- 🔄 **失败重试** — 翻译失败下轮自动重试
- ✂️ **分段翻译** — 长文本自动分段，避免超时
- ⚙️ **灵活配置** — 轮询间隔、模型、语言对、最小文本长度均可配置
- 💻 **跨平台** — Windows、Linux、macOS

## 翻译目标

| 类型 | 字段 | 方式 |
|------|------|------|
| **工单** | `name`、`content` | PATCH 工单 |
| **跟进** | `content` | PATCH 跟进 |
| **任务** | `content` | PATCH 任务 |
| **解决方案** | `content` | PATCH 解决方案 |
| **审批** | `submission_comment`、`approval_comment` | 创建跟进（只读字段） |
| **文档** | — | 跳过（无可写内容） |

## 翻译格式

| 字段 | 格式 |
|------|------|
| **标题** | `原始标题 / Translated title` |
| **描述**（富文本） | `<p>原始内容</p><br><br><p><strong>[AUTO-TRANSLATED]</strong></p><p>翻译内容</p>` |
| **描述**（纯文本） | `原始内容\n\n[AUTO-TRANSLATED]\n翻译内容` |
| **跟进记录** | 与描述相同 — 根据内容类型自动选择富文本或纯文本格式 |

### 示例 — 标题

```
服务器无法连接数据库 / The server cannot connect to the database
```

### 示例 — 富文本描述

```html
<p><strong>生产环境</strong>服务器无法连接到
<span style="color: rgb(255, 0, 0);">MySQL数据库</span>。</p>
<br><br>
<p><strong>[AUTO-TRANSLATED]</strong></p>
<p><strong>Production environment</strong> server cannot connect to the
<span style="color: rgb(255, 0, 0);">MySQL database</span>.</p>
```

### 示例 — 纯文本跟进记录

```
检查了防火墙规则，发现3306端口被意外关闭。

[AUTO-TRANSLATED]
Checked the firewall rules and found that port 3306 was accidentally closed.
```

## 前置要求

- Python 3.9+
- [Ollama](https://ollama.ai/) 已安装并运行
- GLPI 实例已启用 API v2.3 和 OAuth2 认证

## 快速开始

### 方式 A：pip 安装（推荐）

```bash
# 从 PyPI 安装
pip install glpi-followup-translate

# 拉取翻译模型
ollama pull kaelri/hy-mt2:1.8b

# 在当前目录创建配置
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入你的 GLPI 凭证

# 运行
glpi-followup-translate              # 守护进程模式
glpi-followup-translate --once      # 单次执行
glpi-followup-translate -c /path/to/config.yaml  # 指定配置文件
```

### 方式 B：开发 / 源码安装

```bash
# 克隆仓库
git clone https://github.com/qingdaoamerasia/glpi-followup-translate.git
cd glpi-followup-translate

# 可编辑安装（推荐开发使用）
pip install -e .

# 或仅安装依赖
pip install -r requirements.txt

# 拉取翻译模型
ollama pull kaelri/hy-mt2:1.8b

# 配置
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入你的 GLPI 凭证

# 运行
glpi-followup-translate                 # CLI 命令
python -m glpi_followup_translate       # 或通过 python 模块
glpi-followup-translate --once          # 单次执行
```

## 配置说明

复制 `config.yaml.example` 为 `config.yaml` 并编辑：

```yaml
glpi:
  api_url: "http://your-glpi-server/api.php/v2.3"
  auth_method: "oauth2_password"
  client_id: "your_client_id"
  client_secret: "your_client_secret"
  username: "your_glpi_username"
  password: "your_glpi_password"

ollama:
  api_url: "http://localhost:11434"
  model: "kaelri/hy-mt2:1.8b"
  timeout: 60

polling:
  interval: 60          # 轮询间隔（秒）

translation:
  prefix: "[AUTO-TRANSLATED]"
  min_text_length: 0    # 0 = 不限长度
  source_languages:
    - "zh-cn"
    - "zh"
    - "en"
  target_language:
    zh-cn: "en"
    zh: "en"
    en: "zh-cn"

logging:
  level: "INFO"
  file: "glpi-translate.log"
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `glpi.api_url` | GLPI API 地址 | — |
| `glpi.auth_method` | `oauth2_password` 或 `app_token` | `oauth2_password` |
| `glpi.client_id` | OAuth2 Client ID | — |
| `glpi.client_secret` | OAuth2 Client Secret | — |
| `glpi.username` | GLPI 登录用户名（oauth2_password） | — |
| `glpi.password` | GLPI 登录密码（oauth2_password） | — |
| `ollama.api_url` | Ollama API 地址 | `http://localhost:11434` |
| `ollama.model` | 翻译模型 | `kaelri/hy-mt2:1.8b` |
| `ollama.timeout` | 请求超时（秒） | `60` |
| `polling.interval` | 轮询间隔（秒） | `60` |
| `translation.prefix` | 翻译分隔标记 | `[AUTO-TRANSLATED]` |
| `translation.min_text_length` | 最小翻译文本长度（0 = 不限） | `0` |
| `translation.source_languages` | 检测的语言代码 | `["zh-cn", "zh", "en"]` |
| `translation.target_language` | 源→目标语言映射 | `zh-cn→en, zh→en, en→zh-cn` |
| `logging.level` | 日志级别（`DEBUG`、`INFO`、`WARNING`、`ERROR`） | `INFO` |
| `logging.file` | 日志文件路径 | `glpi-translate.log` |

## 测试

### 单工单测试

```bash
python test_single_ticket.py
```

创建一个包含富文本（HTML）内容和中英文混合跟进记录的测试工单，运行一次翻译并验证格式。

脚本将执行：
1. 检查 Ollama 连接
2. 测试 GLPI 认证
3. 创建带 HTML 格式描述的测试工单
4. 添加 3 条跟进记录（纯文本和 HTML 混合）
5. 运行一次翻译
6. 显示翻译结果
7. 验证格式正确性（标题：`/` 分隔符，HTML：`<br>` 分隔符，纯文本：`\n\n` 分隔符）

### 多工单测试

```bash
python test_translate.py
```

创建 3 个中英文混合的测试工单验证端到端翻译。会先清理旧测试工单，然后：

1. 检查 Ollama 和 GLPI 连接
2. 删除已有测试工单
3. 创建 3 个中英文混排的工单
4. 为每个工单添加双语跟进记录
5. 运行一次翻译（启用 DEBUG 日志）
6. 展示所有翻译结果

## 7x24 后台运行

一条命令，自动适配系统：

```bash
glpi-followup-translate --install-service     # 安装
glpi-followup-translate --remove-service      # 卸载
```

| 平台 | 服务 |
|------|------|
| Linux | systemd |
| Windows | 任务计划程序 |
| macOS | launchd |

## 项目结构

```
glpi-followup-translate/
├── glpi_followup_translate/
│   ├── __init__.py
│   ├── __main__.py         # 入口点
│   ├── config.py           # YAML 配置加载
│   ├── glpi_client.py      # GLPI REST API v2.3 客户端
│   ├── main.py             # 守护进程循环、翻译逻辑
│   └── ollama_client.py    # Ollama API 客户端
├── config.yaml.example     # 配置模板（可安全提交）
├── pyproject.toml          # pip 包配置
├── requirements.txt
├── test_single_ticket.py   # 单工单快速测试
├── test_translate.py       # 多工单测试套件
├── README.md
├── README.zh-CN.md
└── CLAUDE.md
```

## License

MIT

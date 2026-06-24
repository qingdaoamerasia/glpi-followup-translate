# GLPI Followup Translate

使用本地 [Ollama](https://ollama.com/) LLM 自动翻译 GLPI 工单。检测中文或英文内容并进行双向翻译（中文 ↔ 英文）。支持工单**标题**、**描述**、**跟进记录**、**任务**、**解决方案**和**审批**。

📖 [English](README.md) | 简体中文

## 功能特性

- 🔄 **守护进程 / 单次执行** — 定时轮询或单次运行
- 🌐 **语言检测** — CJK 比例感知，支持中英混合内容
- 🔀 **双向翻译** — 中文 → 英文，英文 → 中文
- 📖 **术语表** — 专有名词指定翻译，确保术语一致性
- 📝 **保留原文** — 翻译追加到原文后，不覆盖原始内容
- 🎨 **富文本感知** — HTML 格式保留；冗余样式属性自动精简
- 📦 **完整时间线** — 跟进、任务、解决方案、审批（请求 & 回复）
- 🚫 **去重保护** — 内容哈希 + 内嵌标记，双重防重复
- 🔄 **失败重试** — 翻译失败下轮自动重试
- ✂️ **分段翻译** — 长文本自动分段，避免超时
- 📋 **日志查看** — 通过 `--logs` 命令查看运行日志
- ⚙️ **灵活配置** — 轮询间隔、模型、语言对、术语表、最小文本长度均可配置
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
- [Ollama](https://ollama.com/) 已安装并运行
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
glpi-followup-translate --version    # 查看版本
glpi-followup-translate              # 守护进程模式
glpi-followup-translate --once      # 单次执行
glpi-followup-translate --logs      # 查看最近日志
glpi-followup-translate --logs --follow  # 实时跟踪日志
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
glpi-followup-translate --logs          # 查看最近日志
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
  glossary:             # 专有名词术语表（按源语言分组）
    zh-cn:
      工单: "ticket"
      数据库: "database"
      服务器: "server"
    en:
      ticket: "工单"
      database: "数据库"
      server: "服务器"

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
| `glpi.session_dir` | GLPI session 目录路径，用于自动清理（见下方说明） | `""`（禁用） |
| `glpi.session_max_age` | session 文件最大保留时间（分钟） | `2 × polling.interval` |
| `ollama.api_url` | Ollama API 地址 | `http://localhost:11434` |
| `ollama.model` | 翻译模型 | `kaelri/hy-mt2:1.8b` |
| `ollama.timeout` | 请求超时（秒） | `60` |
| `polling.interval` | 轮询间隔（秒） | `60` |
| `translation.prefix` | 翻译分隔标记 | `[AUTO-TRANSLATED]` |
| `translation.min_text_length` | 最小翻译文本长度（0 = 不限） | `0` |
| `translation.source_languages` | 检测的语言代码 | `["zh-cn", "zh", "en"]` |
| `translation.target_language` | 源→目标语言映射 | `zh-cn→en, zh→en, en→zh-cn` |
| `translation.glossary` | 按方向分组的术语表，确保专有名词翻译一致 | `{}`（空） |
| `logging.level` | 日志级别（`DEBUG`、`INFO`、`WARNING`、`ERROR`） | `INFO` |
| `logging.file` | 日志文件路径 | `glpi-translate.log` |

## 测试

```bash
# 仅运行单元测试（无需 GLPI/Ollama）
python test_integration.py --unit

# 单工单快速测试（仅 Round 1）
python test_integration.py --single

# 运行前 N 轮
python test_integration.py --rounds 3

# 运行全部轮次（完整测试）
python test_integration.py --rounds 0

# 查看所有测试轮次
python test_integration.py --list-rounds

# 清理本脚本创建的测试工单
python test_integration.py --cleanup
```

### 单元测试 (`--unit`)

测试语言检测算法、CJK 比例计算、术语表后处理、输出清理和占位符往返替换，无需任何外部服务，速度快且结果确定。

### 集成测试 (`--rounds`)

在真实 GLPI 实例上创建带 `[Test]` 前缀的测试工单，运行翻译并验证输出格式和术语表执行情况。每轮针对特定场景：

| 轮次 | 名称 | 测试内容 |
|------|------|----------|
| 1 | 富文本 HTML + 混合跟进 | HTML 内容、中英文跟进交替 |
| 2 | 短文本 + 长文本 | 极短字符串和多段落长文本 |
| 3 | 低 CJK 比例 | 以英文为主、含少量中文的文本 |
| 4 | 高 CJK 比例 | 以中文为主、含英文技术术语的文本 |
| 5 | 术语表验证 | **动态生成** — 运行时从 `config.yaml` 读取术语表 |
| 6 | 英文 → 中文 | 纯英文工单翻译为中文 |

Round 5 从 `config.yaml` 读取术语表并动态生成测试工单，验证术语是否正确应用，测试文件中不硬编码任何专有名词。

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

## Session 清理（GLPI inode 耗尽防护）

GLPI 的 Symfony 框架会为**每个 API 请求**创建一个 PHP session 文件——即使是无状态的 Bearer token 请求也不例外。如果不清理，这些文件会持续累积，最终耗尽文件系统的 inode，导致 GLPI 崩溃。

配置 `glpi.session_dir` 后，守护进程会在每轮轮询结束后**自动清理**超过 `session_max_age` 分钟的 session 文件。

### 配置步骤

**第一步：检查权限**

守护进程需要对 GLPI 的 session 目录（通常为 `/var/lib/glpi/_sessions`，属主 `www-data:www-data`）有写权限。

```bash
# 查看目录权限
ls -ld /var/lib/glpi/_sessions
# drwxr-xr-x 2 www-data www-data ... /var/lib/glpi/_sessions

# 查看守护进程运行用户
ps aux | grep glpi-followup-translate
```

| 守护进程运行用户 | 能否清理 session？ | 需要的操作 |
|-----------------|-------------------|-----------|
| `root` | ✅ 可以 | 无需操作 — root 绕过文件权限检查 |
| `www-data` | ✅ 可以 | 无需操作 — 与 session 文件同属主 |
| 其他用户（如 `qais`） | ❌ 不可以 | 见第二步 |

**第二步：授权（如需要）**

如果守护进程以非 root、非 www-data 用户运行，需要将其加入 `www-data` 组：

```bash
# 将当前用户加入 www-data 组
sudo usermod -aG www-data $(whoami)

# 授予组写权限
sudo chmod 775 /var/lib/glpi/_sessions

# 重启守护进程使权限生效
sudo systemctl restart glpi-translate.service
```

**第三步：配置**

在 `config.yaml` 中添加：

```yaml
glpi:
  # ... 已有配置 ...
  session_dir: "/var/lib/glpi/_sessions"
  # session_max_age: 30   # 可选，默认 = 2 × polling.interval
```

**第四步：验证**

```bash
# 运行前查看 session 数量
ls /var/lib/glpi/_sessions | wc -l

# 运行几分钟后再查看
ls /var/lib/glpi/_sessions | wc -l
# 应该保持稳定，不再持续增长
```

### 工作原理

1. 每轮轮询，守护进程向 GLPI 发送约 119 个 API 请求
2. 每个请求在 GLPI 服务器上创建一个 PHP session 文件
3. 轮询结束后，守护进程删除超过 `session_max_age` 的 session 文件
4. 结果：session 数量保持稳定，不会无限增长

### 故障排除

| 现象 | 原因 | 解决方法 |
|------|------|---------|
| 日志中出现 `Permission denied` | 守护进程用户无权写入 session 目录 | 见上方第二步 |
| session 数量仍在增长 | `session_dir` 未配置或路径错误 | 检查 `config.yaml` |
| 清理未执行 | 守护进程未使用最新版本 | `pip install --upgrade glpi-followup-translate` |

## 项目结构

```
glpi-followup-translate/
├── glpi_followup_translate/
│   ├── __init__.py
│   ├── __main__.py         # 入口点
│   ├── config.py           # YAML 配置加载
│   ├── glpi_client.py      # GLPI REST API v2.3 客户端
│   ├── main.py             # 守护进程循环、翻译逻辑、日志查看
│   └── ollama_client.py    # Ollama API 客户端
├── config.yaml.example     # 配置模板（可安全提交）
├── pyproject.toml          # pip 包配置
├── requirements.txt
├── test_integration.py     # 统一测试套件（单元测试 + 集成测试）
├── CHANGELOG.md
├── README.md
├── README.zh-CN.md
└── CLAUDE.md
```

## License

MIT

# GLPI Followup Translate

自动翻译 GLPI 工单跟进记录（Followup）的工具。使用本地 Ollama LLM 实现中文 ↔ 英文自动翻译。

## 功能特性

- 🔄 **自动轮询**：定时检查 GLPI 工单的新跟进记录
- 🌐 **语言检测**：自动识别中文/英文内容
- 🔀 **双向翻译**：中文 → 英文，英文 → 中文
- 📝 **保留原文**：翻译结果追加到原文下方，不覆盖原始内容
- 🚫 **去重处理**：已翻译的跟进记录不会重复处理
- ⚙️ **灵活配置**：轮询间隔、翻译参数等均可自定义

## 前置要求

- Python 3.9+
- [Ollama](https://ollama.ai/) 已安装并运行
- GLPI 实例已启用 API v2.3 和 OAuth2 认证

## 安装

### 1. 克隆仓库

```bash
git clone <your-repo-url>
cd glpi-followup-translate
```

### 2. 创建虚拟环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 拉取 Ollama 模型

```bash
ollama pull kaelri/hy-mt2:1.8b
```

### 5. 配置

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，填入你的 GLPI API 凭证：

```yaml
glpi:
  api_url: "http://your-glpi-server/api.php/v2.3"
  client_id: "your_client_id"
  client_secret: "your_client_secret"
```

## 使用方法

### 常驻轮询模式（默认）

```bash
python -m glpi_followup_translate
```

程序将持续运行，每隔配置的间隔时间检查一次新的跟进记录。

### 单次执行模式

```bash
python -m glpi_followup_translate --once
```

执行一次翻译检查后退出。

### 指定配置文件

```bash
python -m glpi_followup_translate -c /path/to/config.yaml
```

### 停止程序

按 `Ctrl+C` 或发送 `SIGTERM` 信号，程序会优雅退出。

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `glpi.api_url` | GLPI API 地址 | `http://your-glpi-server/api.php/v2.3` |
| `glpi.client_id` | OAuth2 Client ID | - |
| `glpi.client_secret` | OAuth2 Client Secret | - |
| `ollama.api_url` | Ollama API 地址 | `http://localhost:11434` |
| `ollama.model` | 翻译模型名称 | `kaelri/hy-mt2:1.8b` |
| `ollama.timeout` | 翻译请求超时（秒） | `60` |
| `polling.interval` | 轮询间隔（秒） | `60` |
| `translation.prefix` | 翻译标记前缀 | `[AUTO-TRANSLATED]` |
| `translation.min_text_length` | 最小翻译文本长度 | `10` |
| `logging.level` | 日志级别 | `INFO` |
| `logging.file` | 日志文件路径 | `glpi-translate.log` |

## 翻译效果示例

**原始跟进记录：**
```
用户报告无法登录系统，提示"密码错误"。
```

**翻译后的跟进记录：**
```
用户报告无法登录系统，提示"密码错误"。

[AUTO-TRANSLATED]
User reports being unable to log into the system, with an "incorrect password" error message.
```

## 项目结构

```
glpi-followup-translate/
├── glpi_followup_translate/
│   ├── __init__.py        # 包初始化
│   ├── __main__.py        # 入口点
│   ├── config.py          # 配置加载
│   ├── glpi_client.py     # GLPI API 客户端
│   ├── main.py            # 主程序逻辑
│   └── ollama_client.py   # Ollama API 客户端
├── config.yaml.example    # 配置模板
├── config.yaml            # 实际配置（已 gitignore）
├── requirements.txt       # Python 依赖
├── .gitignore
├── CLAUDE.md              # 项目开发文档
└── README.md              # 本文件
```

## 测试

运行测试脚本创建示例工单并自动翻译：

```bash
python test_translate.py
```

这会：
1. 检查 Ollama 连接
2. 测试 GLPI 认证
3. 创建 3 个测试工单（中英文混合）
4. 运行一次翻译
5. 显示翻译结果

## License

MIT

# ai_exec

本地跑 LLM 的 Python 工具循环：模型调 `python`，代码在本机执行，结果写回对话。

| 目录 | API |
|---|---|
| `chat_completions/` | OpenAI Chat Completions |
| `messages/` | Anthropic Messages |
| `responses/` | OpenAI Responses |
| `termux_responses/` | 同上，给 Termux / Android |

## 安装

Windows:

```bat
python -m pip install flask psutil requests
```

Termux 见 `termux_responses/README.md`（不要装 `psutil`）。

## 使用

1. 改对应目录的 `input.json`：填 `url`、`headers`、`json.model`。
2. 启动界面：

```bat
cd responses
python viewer.py
```

或双击 `view.vbs`（无控制台）。浏览器打开 `http://127.0.0.1:8765`。

发消息后 `ae.py` 循环请求模型、执行工具，直到模型不再调用。工具进程常驻，状态会保留。

`input.json` 字段：`timeout` 工具超时（秒），`max_tool_output` 输出截断，其余原样 POST。

模型能在本机执行任意 Python。公开仓库前先清掉 `input.json` 里的 key。

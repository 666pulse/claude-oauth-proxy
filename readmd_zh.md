# Claude OAuth Proxy 使用指南

https://mp.weixin.qq.com/s/9DtgaQiFzC8NIKgegDW9SQ

将 Claude 的会员账号变成了 API 使用，纯 Python 标准库，零依赖。

## 前提条件

- Python 3
- Claude Code CLI 2.x

```sh
claude --version
# 确认版本为 2.x.x
```

## 1. 获取 OAuth Token

```sh
claude setup-token
```

将获取到的 token 保存到文件，例如 `~/.claude/oauth.txt`，支持纯文本和 JSON 两种格式。

## 2. 启动代理

```sh
python3 proxy.py -p 3900 -t ~/.claude/oauth.txt

# 通过代理访问
HTTPS_PROXY=http://127.0.0.1:7890 python3 proxy.py -p 3900 -t ~/.claude/oauth.txt
```

参数：`--port/-p`（端口）、`--token-file/-t`（token 路径）、`--log-file/-l`（日志文件）、`--host`（绑定地址）

## 3. 调用 API

```sh
# 健康检查
curl http://127.0.0.1:3900/

# 基本对话
curl -X POST "http://127.0.0.1:3900/v1/messages" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "你是谁"}]
  }'

# 使用工具（Web 搜索）

curl -X POST "http://127.0.0.1:3900/v1/messages" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "max_tokens": 256,
    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    "messages": [{"role": "user", "content": "今天有ai相关的重要新闻么"}]
  }'
```

可用模型：`claude-opus-4-6`、`claude-sonnet-4-6`、`claude-haiku-4-5-20251001`

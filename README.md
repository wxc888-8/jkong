# taotao 失败交易监控 + Telegram 推送

这个项目会监控链上失败交易（历史抓取 + 实时订阅），并推送到 Telegram 群/频道。支持从 `.env` 配置参数，支持管理员私聊机器人热更新部分参数。

## 快速开始（本地）

1. 复制配置文件：

```
cp .env.example .env
```

2. 编辑 `.env`，至少填：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TG_ADMIN_USER_IDS`（管理员的 Telegram 用户ID；先私聊机器人 `/whoami` 获取）

3. 运行：

```
python failed_transactions.py
```

## Docker 运行（宝塔/服务器）

仓库目录内执行：

```
docker compose up -d --build
docker logs -f taotao_failed_tx
```

首次部署要在服务器上创建 `.env`（仓库不会包含 `.env`），推荐：

```
cp .env.example .env
```

然后修改 `.env` 内容再启动。

## 私聊管理（热更新）

在 Telegram 私聊机器人，发送：

- `/help`  查看完整说明
- `/whoami` 获取你的用户ID（用于 `.env` 的 `TG_ADMIN_USER_IDS`）
- `/show`  查看当前参数（token 会打码）
- `/get KEY` 查看某个参数
- `/set KEY VALUE` 修改参数（仅管理员、仅私聊生效，会写回 `.env`）

支持热更新的常用参数（`/set` 可改）：

- `MIN_TAO_THRESHOLD`（阈值）
- `TELEGRAM_CHAT_ID`（推送群，可多个）
- `TG_MIN_INTERVAL_SECONDS`（推送节流）
- `HISTORY_LIMIT`（启动回放条数）
- `PRICE_REFRESH_SECONDS` / `HEARTBEAT_EVERY_BLOCKS` / `PROCESSED_HASHES_MAX` / `PROCESSED_HASHES_KEEP`

说明：

- `TELEGRAM_BOT_TOKEN` 不允许通过私聊修改（安全原因）
- `SUBSTRATE_WSS_URL` 改了通常需要重启进程/容器才会重连生效

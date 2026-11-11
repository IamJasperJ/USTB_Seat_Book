# MCP 接入

服务在 `/mcp` 提供 [Model Context Protocol](https://modelcontextprotocol.io) 接口（Streamable HTTP），可接入 Claude Code、Cursor、Cherry Studio 等 MCP 客户端，让 AI 助手查询和操作**一个账号**的全部预约功能。

- 地址：`http://SERVER:18001/mcp`
- 认证：HTTP Basic，使用本服务的**应用账号和密码**（不是学校统一认证密码）。凭据放在请求头里，不作为工具参数，模型看不到密码。
- 范围：只能操作认证账号自己的数据；不提供管理员功能，不返回图书馆的 Cookie 或 Token，也不能远程签到。

## 客户端配置

先得到 Basic 认证头的值：

```bash
printf 'APP_USERNAME:APP_PASSWORD' | base64
```

Claude Code：

```bash
claude mcp add --transport http ustb-seat http://SERVER:18001/mcp --header "Authorization: Basic <上面的结果>"
```

通用 JSON 配置（Cursor、Cherry Studio 等支持自定义请求头的客户端）：

```json
{
  "mcpServers": {
    "ustb-seat": {
      "type": "http",
      "url": "http://SERVER:18001/mcp",
      "headers": { "Authorization": "Basic <上面的结果>" }
    }
  }
}
```

> 服务是明文 HTTP。Basic 头只是编码、不是加密，请只在内网 / VPN 中使用，或在前面加 HTTPS 反向代理。配置文件里保存的是可还原的密码，注意保管；改密码后旧配置立即失效（最多 5 分钟缓存）。

## 工具

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 总览 | `get_overview` | 配置、登录会话、自动任务、今明两天预约 |
| 登录 | `start_login` | 生成二维码（返回图片），用户用微信扫码 |
| | `login_status`、`list_login_sessions`、`close_login_session` | 查询 / 关闭登录会话 |
| 参数 | `get_config`、`update_config` | 读取和修改预约参数（含自动延后），只传要改的字段 |
| 座位 | `list_areas`、`room_availability`、`seat_slots` | 楼层区域、全区座位可约时段、单座位规则 |
| 预约 | `reserve_seat` | 指定座位和时段；默认 `dry_run` |
| 我的预约 | `my_reservations` | 今天 / 明天的预约及可执行操作 |
| | `cancel_reservation`、`temp_leave`、`rebook_later` | 取消、暂离、取消并延后重约 |
| 自动化 | `automation_status`、`automation_history` | 自动预约任务与历史 |
| | `start_automation`、`stop_automation`、`start_daily`、`stop_daily` | 单次任务和每日模式 |
| | `auto_extend_status`、`auto_extend_check` | 自动延后状态与立即检查（默认预览） |

需要图书馆登录的工具都可以省略 `auth_session_id`，自动使用本账号最新的已登录会话；没有时会提示先调用 `start_login`。

会改变真实预约的工具都标注为“破坏性”（`destructiveHint`），客户端通常会在调用前请你批准；它们和网页一样需要开启 `submit_enabled`，并且要带 `confirm: true`（预约和重约默认只预览）。工具出错时返回与网页相同的中文原因，例如“请明确确认暂离”。

## 实现说明

每个工具在进程内调用本服务对应的 HTTP 接口，并转发同一个认证头，因此校验、权限、确认与错误信息和网页 / 脚本 API 完全一致，详见 [API 文档](API.md)。连续请求时，验证通过的账号密码会在内存中记住 5 分钟（只保存加盐哈希），避免每次都做密码哈希计算，也不会触发每分钟 30 次的认证限流；错误或未验证过的密码仍先受限流。浏览器 Cookie 不能访问 `/mcp`。

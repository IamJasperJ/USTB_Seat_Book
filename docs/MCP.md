# MCP 接入

服务在 `/mcp` 提供 [Model Context Protocol](https://modelcontextprotocol.io) 接口（Streamable HTTP），可接入 Claude Code、Cursor、Cherry Studio 等 MCP 客户端，让 AI 助手查询和操作**一个账号**的全部预约功能。

- 地址：`http://SERVER:18001/mcp`
- 认证：本服务的**应用账号和密码**（不是学校统一认证密码），两种方式任选：
  - **工具参数**：每个工具都有 `username`、`password` 参数，每次调用都带上。适用于任何客户端，包括请求头已被网关的 `Authorization: Bearer …` 占用的情况。密码由模型填写，会出现在对话记录里并发给模型服务商。
  - **HTTP Basic 请求头**：客户端能自定义请求头、且 Authorization 没被占用时可用；这时工具参数里的账号密码可以省略，模型看不到密码。
- 范围：只能操作认证账号自己的数据；不提供管理员功能，不返回图书馆的 Cookie 或 Token，也不能远程签到。

## 客户端配置

**方式一：账号密码作为工具参数（任何客户端都可用）**

只需配置地址；网关需要的 Bearer 头照常配置即可，本服务会忽略它：

```json
{
  "mcpServers": {
    "ustb-seat": {
      "type": "http",
      "url": "https://你的网关/mcp",
      "headers": { "Authorization": "Bearer <网关的令牌>" }
    }
  }
}
```

然后在对话里告诉 AI 本服务的账号和密码（例如写进项目说明或系统提示），它会在每次调用工具时填入 `username` 和 `password`。建议给本服务设一个不与其他网站重复的密码。

**方式二：HTTP Basic 请求头（模型看不到密码）**

```bash
printf 'APP_USERNAME:APP_PASSWORD' | base64
```

```bash
claude mcp add --transport http ustb-seat http://SERVER:18001/mcp --header "Authorization: Basic <上面的结果>"
```

> 服务本身是明文 HTTP；请只在内网 / VPN 中使用，或经 HTTPS 网关访问。改密码后旧密码立即失效（最多 5 分钟缓存）。

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

所有工具都有可选参数 `username`、`password`（见上文认证方式）。需要图书馆登录的工具都可以省略 `auth_session_id`，自动使用本账号最新的已登录会话；没有时会提示先调用 `start_login`。

会改变真实预约的工具都标注为“破坏性”（`destructiveHint`），客户端通常会在调用前请你批准；它们和网页一样需要开启 `submit_enabled`，并且要带 `confirm: true`（预约和重约默认只预览）。工具出错时返回与网页相同的中文原因，例如“请明确确认暂离”。

## 实现说明

每个工具在进程内以该账号身份调用本服务对应的 HTTP 接口，因此校验、权限、确认与错误信息和网页 / 脚本 API 完全一致，详见 [API 文档](API.md)。连续请求时，验证通过的账号密码会在内存中记住 5 分钟（只保存加盐哈希），避免每次都做密码哈希计算，也不会触发每分钟 30 次的认证限流；错误或未验证过的密码仍先受限流。浏览器 Cookie 不能用于 `/mcp`；不带账号密码时可以列出工具，但调用任何工具都会被拒绝。请求头里的 Bearer 令牌（网关用）会被忽略。账号密码不会写入运行日志。

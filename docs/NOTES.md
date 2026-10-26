# autosign 运维笔记

应用侧文档。平台（登录、路由、部署）见仓库根的 `docs/`。

- 数据目录（服务器）：`/var/lib/apps/autosign/`：`autosign.db*`、`secret.key`（丢失则已存的北航密码无法解密，务必随库一起备份）、`audit.log`
- 日志：`journalctl -u app-autosign`；审计 `tail /var/lib/apps/autosign/audit.log`；页面底部“最近动态”来自 `events` 表
- 测试：`cd apps/autosign/backend && python -m unittest discover -s tests -t .`
- 每次对某账号做登录/取课表都是一次**真实的**统一认证登录，不要在循环里反复调用。密码错误/验证码会让账号进入 `auth_failed`/`captcha` 暂停态，需在界面里修改密码或重新开关后恢复。
- 主要接口（均在 `/autosign/api/` 下，需已登录平台）：`GET|POST /accounts`、`PATCH|DELETE /accounts/{id}`、`POST /accounts/{id}/check`、`GET /accounts/{id}/schedule?date=`、`GET /accounts/{id}/week?date=`（该日所在周一至周日，含签到状态与计划时刻；7 次只读课表查询复用已有会话）、`POST /accounts/{id}/sign`、`GET /events`

## 关键行为与不变量（改代码前必读）

1. **签到 POST 只发一次**。响应丢失/异常/不明确 → 只回查课表（`Client._confirm`），不得重放。读取接口仅对连接类故障最多重试 3 次。
2. **HTML/重定向 = 会话失效**，不是“今天没课”。只读调用可自动重登一次；签到不会因会话失效而重发。
3. **调度规则**：账号开启时，每节未签到课程在“课前 10 分钟 → 开课”窗口内随机一个时刻签到（`planned_at` 落库）；账号由关切到开（`kick(force=True)`）时，立即检查，窗口已开（开课前 10 分钟至下课）且未签的课程立即签；已结束的课不安排；`signStatus` 非 0/1 视为未知，不自动提交。**同名同时段**（`Lesson.slot`：名称去多余空白 + 开课时间相同，只是教师/教室不同的多个班）共用同一个随机时刻（`Manager._slot_time`：优先复用已有 pending 计划，其次本轮已定的，最后才新随机）。
4. **登录失败暂停**：`auth`（密码错误）/`captcha` → 账号状态置 `auth_failed`/`captcha`，调度器不再重试，避免触发学校风控。其余错误 5 分钟后重试。
5. **访问控制不在本应用**：登录、会话、防爆破、CSRF 由平台网关（portal + nginx `auth_request`）负责，见仓库 `docs/ARCHITECTURE.md`。本应用只监听 127.0.0.1。
6. **敏感信息**：北航密码只以 Fernet 密文入库，接口永不返回；日志/事件/轨迹不得包含密码、ticket、token、sessionId、完整 URL 查询值（`webvpn.describe()` 只保留参数名）。

---

## WebVPN 登录流程要点（排障必读）

网关 `https://d.buaa.edu.cn`；目标主机编码 = `hex(iv) + hex(AES-CFB(host))`，key=iv=`wrdvpnisthebest!`，格式 `/{https|http}[-端口]/{编码}/原路径`。允许的目标仅：SSO/UC 的 HTTPS 443，iClass 的 HTTPS 8346、HTTPS 8347、HTTP 8081。

真实成功流程（2026-10 实测，任一环被白名单挡住都会报“拒绝不安全的重定向”）：

1. `GET` 网关 SSO `login?service=https://d.buaa.edu.cn/login?cas_login=true` → 200 表单（`execution` 等隐藏字段）。
2. `POST` 表单到网关 SSO → 302 → 门户 `/login?cas_login&ticket`（网关会把它改写成“经网关访问 d.buaa.edu.cn 自己”的形式，主机编码解出来是 `d.buaa.edu.cn`，**仅放行** `/login`、`/logout`、`/wengine-vpn-token-login`、`/token-login`）。
3. → `/wengine-vpn-token-login?token` → `/token-login?token` → 门户首页 `/`（到首页即停止，不抓取）。此后 CookieJar 才有网关会话。
4. `GET` iClass `https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter` → SSO（已登录）→ 带 ticket 回到 8346 → 若干 302 → 某一跳的 Location 含 `loginName=<长串>`（**不是学号**，且很长；取 `\S{1,512}`），最终 200。
5. `GET` `https://iclass.buaa.edu.cn:8347/app/user/login.action?phone=<loginName>&verificationType=2&userLevel=1&password=&verificationUrl=` → `result.id`、`result.sessionId`。若用学号代替 loginName，会得到 `STATUS=1`。
6. 业务接口：`Sessionid` 请求头 + `id` 参数；签到走 `http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action`（POST，`courseSchedId`/`timestamp` 在查询参数，`id` 在表单体）。

排障方法：登录失败时，“最近动态”或 `GET /autosign/api/accounts/{id}/schedule` 的错误里带有脱敏轨迹（`方法 目标路径?[参数名] -> 状态 => 下一跳`）。对照上面流程找出哪一跳被拒/断，再决定是放行新的门户路径（加到 `PORTAL_PATHS`，要保持最小放行）还是修解析。**不要**为了省事放开任意 `d.buaa.edu.cn` 路径或任意主机。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 账号状态 `auth_failed` | 密码错/被学校锁；在界面编辑密码并重新开启，勿反复重试 |
| 账号状态 `captcha` | 统一认证要求验证码；让用户先在浏览器正常登录一次再开启 |
| `STATUS=1` 来自 `login.action` | 多半是 loginName 没取到而回退为学号；看轨迹中“loginName 来源” |
| 入口页显示“未运行” | `systemctl status app-autosign`、`journalctl -u app-autosign -n 100` |

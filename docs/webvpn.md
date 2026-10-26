# 北航 WebVPN 登录与手动签到实现说明

基于 buaa-sign-python 项目实现整理  ·  2026 年 10 月 18 日

本文面向需要理解或移植客户端的开发者，说明校外访问 iClass 的地址映射、统一认证、课表查询和手动签到流程。示例仅使用学校公开服务地址，不包含个人账号、密码、Cookie 或业务令牌。

### 一 访问原理

客户端通过学校 WebVPN 网关访问指定的校内 Web 服务。这里的 WebVPN 是 HTTP 应用代理：请求先到达网关，再由网关访问目标服务；它不建立操作系统级的全局 VPN 隧道，也不改变设备全部流量的路由。账号仍须具备学校服务访问权限。

```text
客户端 → https://d.buaa.edu.cn → 学校 SSO / iClass
                          ↓
                网关 Cookie 与 iClass 业务会话
```

业务层仍使用 iClass 的逻辑地址；WebVPNTransport 在发送请求前统一调用 gateway_url 转换地址。因此，课表和签到逻辑可在 direct 与 webvpn 两种网络模式间复用。

### 二 地址映射与边界

网关地址的结构为“网关域名 / 协议及端口 / 主机编码 / 原路径”。查询参数保留在原请求中。例如：

```text
https://iclass.buaa.edu.cn:8347/app/…
→ https://d.buaa.edu.cn/https-8347/<主机编码>/app/…
```

webvpn.py 内置学校目标主机的固定编码。这些编码是网关 AES-CFB 地址表示的一部分，不是账号密钥，也不能替代用户认证。当前实现不在运行时计算任意主机的编码，而是仅允许已知的学校服务。

允许的目标包括 SSO 与 UC 的 HTTPS 443，以及 iClass 的 HTTPS 8346、HTTPS 8347 和 HTTP 8081。门户自身仅允许已知登录、退出和回调路径。所有认证跳转均先验证目标，再继续访问；带用户名密码的 URL、异常字符、陌生主机或端口会被拒绝。

客户端到 WebVPN 网关使用 HTTPS 并验证证书。签到服务的逻辑上游使用 HTTP 8081，因此不能把这一链路描述为从客户端一直到校内应用的端到端 HTTPS。

## 统一认证与业务会话

### 三 登录分为两个阶段

第一阶段建立 WebVPN 访问会话；第二阶段进入 iClass，取得业务接口所需的用户标识和会话标识。只有拿到网关 Cookie，并不等于已经完成 iClass 业务登录。

1. 通过网关访问 https://sso.buaa.edu.cn/login，service 参数指向 WebVPN 门户登录回调。手动跟随经过校验的重定向，最多允许 12 次跳转。

2. 解析统一认证 HTML 表单，读取 execution 等隐藏字段，并保留表单原有字段。遇到验证码或额外认证提示时停止，交由用户处理；当前客户端不提供验证码交互流程。

3. 将 form action 解析为正确目标。未被网关改写的根相对路径，需要基于逻辑 SSO 地址解析，不能直接拼到网关根目录。发送凭据前，验证表单来源和目标均属于学校 SSO。

4. 向已验证的表单目标提交 username、password 和隐藏字段。使用同一 CookieJar 维护认证状态。凭据 POST 若返回 307 或 308，客户端停止处理，避免将密码表单重放到新地址。

5. 认证成功后，通过网关访问 iClass 的 HTTPS 8346 入口，带 type=jumpMyCenter。检查跳转地址并提取 loginName，作为下一步业务登录参数。

6. 通过网关调用 HTTPS 8347 上的 /app/user/login.action。请求参数包括 phone=loginName、verificationType=2、userLevel=1，以及空 password 和 verificationUrl。该空密码参数用于已有统一认证结果的业务交换，并非绕过学校认证。

7. 校验响应 STATUS 和 result 字段，取得 result.id 与 result.sessionId。后续接口使用 id 参数及 Sessionid 请求头。缺少任一标识时，不保留不完整的登录状态。

### 四 会话状态分别管理

CookieJar 保存网关与认证相关 Cookie；Client.user_id 和 Client.session_id 保存 iClass 业务身份。重新登录前清理旧身份，失败时不能继续使用上一次账号的业务标识。

调用业务接口时，如果返回登录页面、表单 HTML 或重定向，WebVPNTransport 将其识别为会话失效或需要额外认证，不把它误当成空课表，也不自动重放业务请求。用户需要重新登录。

核心 Client 不负责持久化密码。调用方是否保存账号信息，是独立的产品设计；本文的最小示例只在交互登录时读取密码。Cookie 和 sessionId 同样具有认证价值，不应进入日志或被转发。

## 课表读取与手动签到

### 五 查询课表

当日课表调用 /app/course/get_stu_course_sched.action，参数 dateStr 使用 YYYYMMDD。WebVPN 模式下，查询请求使用 GET，并附带用户 id 和 Sessionid 请求头。

课表解析保留课程安排 ID、名称、教师、教室、起止时间和签到状态。signStatus 为 0 表示未签到，1 表示已签到；其他取值标记为未知，不猜测为未签到。全部业务时间按 Asia/Shanghai 处理。

整学期课表先通过 /app/choosecourse/get_myall_course.action 读取课程，再调用 /app/my/get_my_course_sign_detail.action 获取教学日期，最后逐日查询完整安排。详情未可靠提供结束时间和教室时，不自行推算。课程目录为空时，可按明确的学期日期范围逐日查询。

只读列表请求仅对连接类故障进行有限重试，最多 3 次。认证失败、业务错误、响应结构异常不走该重试路径；异常不能直接变成“今天没课”。

### 六 用户点击签到后的处理

1. 重新查询该课程当天的最新安排，而不是直接使用界面缓存。课程必须仍存在、明确未签到，并满足客户端时间判断。当前代码判断区间为课前 10 分钟至课程结束；学校是否实际开放仍以服务端为准。

2. 调用 /app/common/get_timestamp.action，获取学校服务返回的时间戳。校验业务状态，并确认时间戳为 10 至 16 位数字；获取失败则不提交。

3. 再次检查时间范围，然后提交 /app/course/stu_scan_sign.action。WebVPN 模式使用 POST：courseSchedId 与 timestamp 放在查询参数，用户 id 放在表单体，Sessionid 放在请求头。签到逻辑上游为 http://iclass.buaa.edu.cn:8081。

4. STATUS=0 且 result.stuSignStatus=1 时确认成功。若响应丢失、异常或未给出明确成功标记，只重新查询课表核实结果，不再次提交签到 POST。无法确认时返回 unknown，提示用户查看 iClass 记录。明确的业务失败返回 failed。

### 七 为什么不重发签到请求

网络超时只能说明客户端没有收到结果，不能证明服务器未执行。直接重试可能重复提交。因此代码将读取请求与签到写入区别处理：读取可有限重试，签到提交保持一次，之后仅回查状态。这并不等于网络故障下能够保证成功。

## 移植入口与验证要点

### 八 最小调用示例

以下示例先查询，再由用户选择课程并确认后手动提交。密码通过终端交互输入，不写入示例配置文件。

```python
from datetime import datetime
from getpass import getpass
from buaa_sign.client import Client, CHINA

client = Client(network="webvpn")
number = input("学号: ").strip()
password = getpass("统一认证密码: ")
try:
    client.login(number, password)
finally:
    password = None

rows = client.schedules(datetime.now(CHINA).date())
for i, lesson in enumerate(rows):
    print(i, lesson.name, lesson.start, lesson.signed)

if rows:
    index = int(input("选择课程序号: "))
    if not 0 <= index < len(rows):
        raise ValueError("课程序号不合法")
    if input("确认手动签到? 输入 YES: ") == "YES":
        result = client.sign(rows[index])
        print(result.status, result.message)
```

生产界面应处理 ClientError、无课情况和输入错误。将 password 设为 None 只是释放变量引用，并不保证底层内存立即清零。请勿记录完整请求 URL、响应正文、认证表单或会话令牌。

### 九 代码定位

buaa_sign/webvpn.py：地址白名单、逻辑地址还原、网关地址生成和认证表单解析。
buaa_sign/client.py：HTTP 传输、Cookie 管理、双阶段登录、课表解析、手动签到及结果回查。
tests/test_webvpn.py：网关映射、认证跳转、会话失效和提交不重放的回归测试。
tests/test_client.py 与 tests/test_read_retries.py：业务解析、签到校验和读取重试测试。

### 十 验证重点与适用范围

重点验证错误主机和端口被拒绝、错误表单目标不收到密码、307/308 不重放凭据、验证码触发停止、登录失败清理旧身份、HTML 登录页不被当成 JSON，以及签到响应丢失后仅回查。可以运行 python3 -m unittest discover 执行项目单元测试。

本文描述当前项目实现，不是学校发布的接口规范。网关映射、认证页面、业务字段或访问策略变更后，需要重新适配；单元测试通过不能替代真实账号、网络和学校服务的兼容性验证。

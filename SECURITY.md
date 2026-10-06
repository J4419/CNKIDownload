# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when available. Do not open a public
issue containing credentials, institution login details, session cookies, signed download URLs,
or an affected browser profile. If private reporting is unavailable, open a minimal issue asking
the maintainers for a private contact channel.

Include the affected version, operating system, a minimal reproduction, and the security impact.
Remove literature lists, institution names, proxy credentials, cookies, access tokens, and local
paths unless they are essential and safe to disclose.

## 访问边界（请先读这一段）

本工具**不**包含也不会接受任何绕过访问控制的功能。具体来说，以下改动一律不会被合并：

- 模拟点击、自动填写验证码、破解或绕过知网滑块验证；
- 伪造请求头 / 签名以获取无权访问的全文；
- 绕过机构订阅校验、并发限流或反爬机制的代码。

它的设计前提就是**最后一下「PDF下载」由真人点击** —— 这既是它能稳定工作的原因，
也是它的合规边界。请只下载你被授权、且确有研究需要的材料。

## Sensitive local data

调试用的浏览器 profile 位于用户数据目录（Windows 为
`%LOCALAPPDATA%\CNKIDownload\browser-profile`），里面有机构登录态和 Cookie。
请像对待一个浏览器账号那样对待它：

- 不要提交到 git，不要打进发布包，不要上传用于排查问题；
- 共享电脑上用完请关掉浏览器窗口；
- 需要彻底重置时用 `python 4.ClearProfile.py --all`（会连登录态一起清掉）。

`cnki-state/` 里保存着你的文献清单、检索进度和 URL 缓存，`--report` 生成的对账报告
还会包含文献标题。这些都会**暴露你的研究课题**。`run.log` / 终端截图同理。
公开任何东西之前请先检查。

## CDP debugging endpoint

远程调试端口只绑定 `127.0.0.1`，且启动前会用 `SO_EXCLUSIVEADDRUSE` 探测端口是否空闲，
被占用就直接报错、不会硬上。**不要把 `--remote-debugging-address` 改成对外地址** ——
那等于把整个浏览器的控制权（含 Cookie、登录态）暴露给同网段。

`--no-launch` 模式下程序只连接已存在的端口，同样只连回环地址。

## 本机代理

若环境里设了 `HTTP_PROXY` / `HTTPS_PROXY` 且没有 `NO_PROXY`，对 `127.0.0.1:9222` 的请求
会被代理转发，表现为 `Unexpected status 502 ... does not look like a DevTools server`。
本工具在发起任何请求前会把回环地址写进 `NO_PROXY`，并用 `ProxyHandler({})` 显式旁路代理，
因此不会把调试端口流量送到外部代理。诊断输出不会显示代理的账号密码。

## Cache cleanup

`4.ClearProfile.py` 默认只删除可再生的缓存目录，Cookies / Login Data / Preferences
一律不动。`--all` 才会删除整个 profile，并且：

- 拒绝在磁盘根目录、用户主目录、工具目录本身、系统个人文件夹和过浅路径上执行；
- 对自定义 `--profile` 执行 `--all` 时必须额外加 `--confirm-custom-profile`。

不熟悉目标时请先跑 `--dry-run`。

## Scope

This project automates access the user already possesses. Security reports should not include
methods intended to evade publisher access controls, rate limits, verification systems, or
subscription requirements.

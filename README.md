# 多平台视频解析与下载

一个轻量的网页下载工具，页面只保留一条流程：

```text
粘贴链接 → 分析 → 预览 → 下载
```

程序会自动识别抖音、小云雀、小红书和快手的分享链接或包含链接的分享文字，准备当前会话可以访问的视频文件，并提取平台发布文案和视频中的讲话文稿。声音会标记为“讲话”“讲话 + 音乐/背景声”“音乐/其他声音”或“无声音”，然后在网页中显示处理进度、视频预览、文案和下载按钮。

> 下载的是平台当前向该会话提供的最高可访问质量视频流，不等同于创作者上传前的母版，也不保证每条作品都无水印。程序不会绕过私密、付费、好友可见、地区限制或其他访问控制。请只下载你拥有或已获明确授权的内容，并遵守平台规则与适用法律。

## 一键启动

在 Windows 中双击 [start.cmd](./start.cmd)。首次运行会自动创建项目独立的 Python 3.12 环境并安装所需组件，随后打开网页。

默认端口从 `8765` 开始；如果被占用，程序会依次尝试 `8766` 至 `8775`。关闭启动窗口即可停止服务。

本机访问地址通常是：

```text
http://127.0.0.1:8765
```

如果浏览器没有自动打开，请查看启动窗口中显示的实际地址，并手动复制到浏览器。任务数据库和下载文件保存在 `data/` 中。

## 使用方法

1. 将任一支持平台的短链、作品链接或包含链接的分享文字粘贴到输入框。
2. 点击“分析”。
3. 等待进度条完成。
4. 在预览屏幕中确认视频，并查看声音类型、发布文案与讲话文稿。
5. 点击“下载”保存视频；文案可直接复制。

一次只处理当前页面中的一个任务。网页不需要选择模型、字幕、语言或其他处理参数；程序先用高置信讲话检测区分正常讲话和非讲话声音，只转写讲话时间段。平台字幕可用且检测到讲话时优先使用，否则自动使用本机 Medium 中文语音模型，并过滤低置信、长停顿和重复幻听结果。首次识别讲话会下载约 1.5 GB 的模型；之后可离线复用。纯音乐、静音或人声不清晰的视频不会生成错误的讲话文稿，但不影响视频预览和下载。轻量分析无法把所有非讲话声音严格区分为音乐、演唱、环境声或音效，因此页面会如实标为“音乐/其他声音”。不同浏览器会话的任务与视频文件相互隔离，页面也不提供全局处理历史。

## 本机与局域网访问

服务默认绑定 `0.0.0.0`，因此具备局域网访问条件；本机仍应使用 `127.0.0.1` 打开。局域网中的其他设备使用运行电脑的 IPv4 地址，例如：

```text
http://192.168.1.20:8765
```

程序会自动识别本机当前的接口地址；使用自定义域名、代理主机名或自动识别不到的地址时，应把它加入 `DOUYIN_ALLOWED_HOSTS`。双击“开启局域网共享”会把端口限制为仅允许 `LocalSubnet` 来源访问；仍不要在不可信的公共 Wi-Fi 上开启共享。

以下 PowerShell 示例仅在当前终端会话中生效：

```powershell
$env:DOUYIN_APP_HOST = '0.0.0.0'
$env:DOUYIN_APP_PORT = '8765'
$env:DOUYIN_PUBLIC_URL = 'http://192.168.1.20:8765'
$env:DOUYIN_ALLOWED_HOSTS = '127.0.0.1,localhost,192.168.1.20'
$env:DOUYIN_BROWSER_FALLBACK = '1'
.\start.cmd
```

如果不希望启动时自动打开浏览器，可同时设置：

```powershell
$env:DOUYIN_NO_BROWSER = '1'
```

## 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `DOUYIN_APP_HOST` | `0.0.0.0` | 服务监听地址。仅允许本机访问时可设为 `127.0.0.1`。 |
| `DOUYIN_APP_PORT` | 未指定时从 `8765` 开始自动选择 | 固定监听端口；设置后如果该端口被占用，启动会失败。 |
| `DOUYIN_PUBLIC_URL` | 空 | 用户实际访问的完整外部地址，例如 `http://192.168.1.20:8765` 或 `https://video.example.com`。不要包含额外路径。 |
| `DOUYIN_ALLOWED_HOSTS` | localhost、本机名和检测到的接口地址 | 额外允许访问服务的 Host，多个值用英文逗号分隔；填写主机名或 IP，不要填写协议和路径。 |
| `DOUYIN_BROWSER_FALLBACK` | `1` | `1` 表示快速解析失败时允许调用服务器上的独立 Edge；`0` 表示禁用。公网无人值守部署建议设为 `0`。 |

修改环境变量后需要重新启动服务。

抖音浏览器兜底和剪映公开页面解析依赖运行服务器上的 Windows 与 Edge；抖音遇到平台校验时可能弹出需要人工操作的独立窗口，所以适合本机或受信任的局域网环境。它不会让远程访客直接操作该窗口。公网服务不要使用个人登录资料或个人 Cookie 为陌生用户解析内容。

## 公网部署

不要将 Uvicorn 端口直接暴露到互联网。公网部署必须使用 Nginx、Caddy、IIS 或同类反向代理提供 HTTPS，并让应用只监听反向代理可访问的本机或私有网络地址。

示例环境配置：

```powershell
$env:DOUYIN_APP_HOST = '127.0.0.1'
$env:DOUYIN_APP_PORT = '8765'
$env:DOUYIN_PUBLIC_URL = 'https://video.example.com'
$env:DOUYIN_ALLOWED_HOSTS = 'video.example.com,127.0.0.1,localhost'
$env:DOUYIN_BROWSER_FALLBACK = '0'
$env:DOUYIN_NO_BROWSER = '1'
```

反向代理应保留正确的 `Host`，并传递可信的客户端地址与 `X-Forwarded-Proto: https`；只信任实际反向代理的转发头。视频预览和下载还需要保留 HTTP Range 请求，不要让共享 CDN 缓存不同用户的任务文件。

在向公众开放前，还必须补齐并验证以下运营保护：

- 按 IP 或用户限制分析频率和同时进行的任务数；
- 设置单文件、单用户、每日流量及全局磁盘配额；
- 为任务和下载文件设置 TTL，定时删除过期记录与产物；
- 监控磁盘、队列、失败率和上游限流状态；
- 在现有匿名会话隔离之上，根据实际使用场景增加登录、访问控制和滥用处理机制。

当前实现采用单进程任务队列、SQLite 和本机文件目录，只适合个人、局域网或小规模低并发使用。不要通过启动多个 Uvicorn Worker 来直接扩容；多个进程会各自持有队列，并可能互相中断任务。更大规模的部署应拆分 Web 与下载 Worker，使用持久任务队列、独立数据库和共享对象存储。

## 开发与测试

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe scripts\serve.py
```

下载与解析主要基于：

- [yt-dlp 嵌入式调用](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)
- [yt-dlp Douyin 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py)
- yt-dlp XiaoHongShu 提取器
- 即梦、小云雀和快手的公开分享页数据
- [Playwright 持久化浏览器会话](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)

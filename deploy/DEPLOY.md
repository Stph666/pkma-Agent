# 部署说明（Windows + Uvicorn）

这个项目是 **Python / FastAPI**，没有 Java 组件，**不需要 Tomcat**。
Tomcat 是 Servlet 容器，与本站无关；Apache / Nginx 在本项目里只是可选的"统一入口 + 反向代理"。

**本机现状（2026-11-06）**：未安装 Apache / Nginx，验收采用**直连模式**——Uvicorn 直接监听 `0.0.0.0:8000`，局域网可访问。

> 本文件位于代码仓库的 `deploy\` 目录；运行日志写到仓库外的 `logs\`（可用 `HOST`/`PORT` 调整）。

## 架构

```
浏览器 ──► Uvicorn(0.0.0.0:8000) ──► MySQL / Chroma / 本地模型
                 ▲
                 └── 可选：Apache(8080) / Nginx(80) 反向代理
```

## 一、准备

| 项 | 要求 |
|---|---|
| Python | 3.12（项目 `.venv` 已建好） |
| MySQL | 9.x，库名 `pkma`，账号见 `.env` 的 `DATABASE_URL` |
| 磁盘 | 模型缓存 `data/models` 约 4GB（bge-m3 + reranker） |
| 显存 | 8GB 够用（fp16，两个模型常驻约 3GB） |
| 网络 | 首次启动前确认模型已下载；之后可离线运行 |

```cmd
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

首次启动会预热向量模型与重排模型（日志出现"向量模型预热完成""重排模型预热完成"），约 15–30 秒。

## 二、启动 / 停止

```cmd
REM 只给本机用
deploy\start.cmd

REM 局域网可访问（答辩时同网段的手机/笔记本可以打开）
set HOST=0.0.0.0
deploy\start.cmd
```

- 前台运行会阻塞窗口；关窗口即停服。
- 日志追加到 `..\logs\server.log`（在代码目录之外）。
- 启动前会检查端口占用，避免"启动成功但访问的是旧进程"。
- 停止：`deploy\stop.cmd`（默认 8000，可用 `set PORT=xxxx` 指定）。

> 单进程部署的原因：向量模型与重排模型常驻显存，多 worker 会各加载一份，8GB 显存放不下。

### 注册为开机自启（可选）

任务计划程序 → 创建任务 → 触发器"登录时" → 操作启动 `deploy\start.cmd`。

## 三、反向代理（可选，本机未安装）

`deploy/apache-vhost.conf.example` 已给出 Apache 配置：8080 → 8000。
关键三行（SSE 流式输出必须关闭缓冲，否则回答会"憋一大段再吐"）：

```apache
ProxyPass        / http://127.0.0.1:8000/ retry=0 timeout=300
ProxyPassReverse / http://127.0.0.1:8000/
SetEnv proxy-sendchunked 1
```

Nginx 等价配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_buffering off;
    proxy_read_timeout 300s;
    proxy_set_header Host $host;
}
```

## 四、验收（可重复执行）

```cmd
.venv\Scripts\python.exe scripts\acceptance.py http://127.0.0.1:8000
```

脚本逐项打真实接口，输出 PASS/FAIL：

| 检查 | 期望 |
|---|---|
| 健康检查 | `database` / `vector_store` 均 ok |
| 文档列表 | 有 ready 文档（当前 14 份） |
| 全文搜索 | 从真实历史取关键词，命中 > 0 |
| 图谱数据 | 返回节点与边 |
| 整节上下文 | 返回 >200 字的整节内容与页码 |
| 前端入口 | 200 且为 React 入口 |
| 流式问答 | 事件流含 delta 与 done |

**2026-11-06 实测结果：7/7 通过**（改用 `HOST=0.0.0.0` 后，同网段另一台设备访问同样返回 200）。

## 五、局域网访问与防火墙

1. 启动时设 `HOST=0.0.0.0`（默认 `127.0.0.1` 只给本机）。
2. 查本机 IP：`ipconfig`，找到形如 `192.168.x.x` 的 IPv4 地址。
3. 同一网段访问 `http://<本机IP>:8000`。

若别的设备打不开，多半是 Windows 防火墙拦了（需管理员权限，执行一次即可）：

```cmd
netsh advfirewall firewall add rule name="PKMA 8000" dir=in action=allow protocol=TCP localport=8000
```

## 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 页面能开但提问报 500 | 模型未下载或 `HF_HUB_OFFLINE` 残留，看 `..\logs\server.log` |
| 第一个问题特别慢（15s+） | 预热线程还没跑完；等日志出现"重排模型预热完成" |
| 流式答案卡住不动 | 反代开了缓冲，关掉 `proxy_buffering` |
| 检索全被判"无依据" | 检查 `.env` 的 `RERANK_ENABLED`；关掉时阈值自动切到 0.60 兜底 |
| 上传报 Chroma 相关错误 | `CHROMA_DIR` 必须在纯英文路径（例如 `D:/chroma-data`） |
| `deploy\start.cmd` 输出乱码/报"不是内部命令" | `.cmd` 必须是 ASCII 或 GBK 保存；当前版本已改为纯 ASCII |

## 七、降级开关

答辩机器配置不够时，改 `.env` 后重启：

```ini
RERANK_ENABLED=false     # 关重排，检索快 1.5s，阈值自动切到 0.60
EMBEDDING_DEVICE=cpu     # 无显卡时改 CPU（会明显变慢）
```

关重排不会乱答：`retrieval.py` 有 `rerank_missing_floor` 兜底，实测"红烧肉怎么做"仍会被拒答。

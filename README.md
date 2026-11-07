# 个人知识管理智能体（PKMA）

基于 LLM 的本地知识库问答系统：上传 PDF / DOCX / TXT → 自动解析、切片、建向量索引 → 支持带引用的问答、知识图谱与原文定位。

课程设计第 22 题，FastAPI + React，全部本地推理（向量与重排模型都在本机 GPU 上跑）。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Uvicorn（单进程） |
| 存储 | MySQL 9 + SQLAlchemy 2 · Chroma（向量） |
| 模型 | `BAAI/bge-m3`（1024 维）· `BAAI/bge-reranker-base`（重排）· OpenAI 兼容 LLM |
| 前端 | React 18 + TypeScript + Vite · ECharts（图谱） |
| 解析 | PyMuPDF（PDF）· python-docx（DOCX） |

## 目录

```
pkma\                  项目代码（本文件所在目录）
├─ app\                后端：api / services / integrations / ui
├─ web\                前端源码与构建产物（web\dist 由后端托管）
├─ tests\              单元 + 集成测试
├─ scripts\            运维脚本（验收、基准、重建向量）
├─ deploy\             启动停止脚本、部署说明、Apache 反代样例
├─ data\               上传原文件、样例、模型缓存
└─ .venv\ .env requirements.txt pytest.ini

..\课程资料\            方案、模板、开发文档（不在代码里）
..\logs\               运行日志与验收截图
```

## 快速开始

```cmd
REM 1. 依赖（.venv 已存在时跳过）
.venv\Scripts\python.exe -m pip install -r requirements.txt

REM 2. 配置：复制 .env.example 为 .env，填 DATABASE_URL 与 LLM 参数

REM 3. 前端构建（dist 是构建产物，不入库）
cd web
npm install
npm run build
cd ..

REM 4. 初始化数据库（首次）
.venv\Scripts\python.exe scripts\init_db.py

REM 5. 启动
deploy\start.cmd
```

打开 **http://127.0.0.1:8000**。首次启动会预热向量与重排模型（约 15–30 秒，日志出现「重排模型预热完成」后即可提问）。

停止：`deploy\stop.cmd`　局域网访问：`set HOST=0.0.0.0` 后启动。详见 `deploy\DEPLOY.md`。

## 核心能力

| 能力 | 说明 |
|---|---|
| 入库 | 父子分块（子片 120–160 token 检索、父块 480 token 供模型），入库前字数闸门 |
| 检索 | 向量 + BM25 融合、查询改写、图谱一跳扩展、交叉编码器重排 |
| 问答 | 三档判据（有依据 / 部分相关 / 拒答），回答带 `[n]` 引用并可定位到整节原文 |
| 图谱 | 实体 / 关系 / 共现三类边，ECharts 力导向，点击节点在右侧阅读面板打开整节 |
| 阅读 | 命中片段按父块前后扩到约 2600 字，遇标题停，命中段落高亮 |

## 关键配置

| 项 | 值 |
|---|---|
| 向量模型 | `BAAI/bge-m3`，1024 维 |
| 重排 | `BAAI/bge-reranker-base`，`RERANK_ENABLED=true` |
| 判档阈值 | 重排 ≥0.5 有依据 / ≥0.2 部分相关；重排不可用时退回向量 0.60 |
| 候选池 | 40（为重排预留，未接重排时调大只增加延迟） |
| 上下文 | `context_top_k=5`，单文档上限 1200 页 / 12000 片 |

## 测试与验收

```cmd
.venv\Scripts\python.exe -m pytest                                     REM 112 项
.venv\Scripts\python.exe scripts\acceptance.py http://127.0.0.1:8000   REM 部署验收 7 项
.venv\Scripts\python.exe scripts\bench_retrieval.py --rewrite          REM 检索基准 8 题
.venv\Scripts\python.exe scripts\check_phase_e.py                      REM 数据一致性自检
```

## 文档

| 文档 | 位置 |
|---|---|
| 部署、验收、降级开关 | `deploy\DEPLOY.md` |
| 问题记录（36 条，现象/原因/解决） | `..\课程资料\开发文档\问题记录.md` |
| 开发日志（按时间的改动与验证） | `..\课程资料\开发文档\开发日志.md` |
| 环境参数与关键决策 | `..\课程资料\开发文档\环境与决策.md` |
| 开发过程记录（阶段性方案） | `..\课程资料\开发文档\开发过程.md` |

> 上表中的开发文档与课程材料**不在本仓库内**——它们与代码分离存放，随课设提交材料一并提供。
> 仓库只保留运行本项目所需的代码、配置与部署说明。

## 已知限制

- 拒答时仍会展示「最相关的片段」，容易被误读成有依据
- 跨语言检索（中文问英文书）依赖模型语义对齐，极端情况会落到索引页
- 单进程部署：模型常驻显存，多 worker 会各加载一份
- 暂不支持扫描件 OCR（纯图片 PDF 会被判定为不可解析）

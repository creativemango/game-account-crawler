# 账号交易平台爬虫 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** 实现 pxb7.com 和 [已移除].com 账号爬虫，定时轮询 + SQLite 存储 + FastAPI 查询

**Architecture:** Scrapling Fetcher 发 HTTP → 解析 JSON → SQLite 去重存储 → FastAPI 暴露查询接口。`while True + sleep` 调度。

**Tech Stack:** Python 3.12+, Scrapling, SQLite, FastAPI, uvicorn, PyYAML

**Target files:** 8 files, zero prior code.

---

### Task 1: 项目骨架

**Files:** `requirements.txt`, `config.yaml`, `crawler/__init__.py`

写入所有依赖和默认配置。

### Task 2: SQLite 存储层

**Files:** `db.py`

创建表、插入/更新、查询方法。

### Task 3: [已移除].com 爬虫

**Files:** `crawler/[已移除].py`, `crawler/base.py`

GET API → 解析 products[] → 返回 dict 列表。

### Task 4: pxb7.com 爬虫

**Files:** `crawler/pxb7.py`

POST API → 解析 data.list[] → 返回 dict 列表。

### Task 5: 调度主程序

**Files:** `main.py`

读取配置 → 循环爬取 → 入库。

### Task 6: FastAPI 查询接口

**Files:** `api.py`

搜索/详情/统计接口。

### Task 7: 验证

启动 api.py → curl 测试接口 → 确认数据入库。


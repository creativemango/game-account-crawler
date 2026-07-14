"""向前爬虫: 按源向前翻页爬取历史账号数据

两个独立 CLI 脚本:
  - python -m backfill.pxb7 --game-id 10302 --start-page 4 --max-pages 50
  - python -m backfill.pzds --game-id 303 --start-page 4 --max-pages 50

职责: 列表→详情→解析→特征→入库 (不计算价值, 交 main.py valuer_loop 补全)
"""

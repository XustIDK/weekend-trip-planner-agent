"""5 层记忆：账本(只追加) → 窗口 / 元数据 / 档案卡 / 摘要 / 归档检索。

对应笔记「Part 2 4 层核心 + 1 层向量兜底」：
  第 1 层 窗口      window.recent()
  第 2 层 元数据    session_meta.build()
  第 3 层 档案卡    profile.merge()
  第 4 层 摘要      summarizer.roll()
  第 5 层 归档检索  archive.Archive.search()
账本本身对应笔记 3.2 的 Ledger：只追加，给上面几层提供可重算的底稿。
"""

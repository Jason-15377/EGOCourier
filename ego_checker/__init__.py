"""EGOChecker 分析引擎（内嵌于 EGO Courier）。

只保留分析内核：数据发现与读取（CSV / MCAP / WAV 音频 / 视频）、
同步与丢帧检查、跨段边界、元数据完整性，以及自包含 HTML 报告生成。
本包不提供独立 GUI 与 CLI；界面由 EGO Courier 的「日志分析」页提供，
分析参数与结果表格沿用 EGO Courier 的统一界面风格。
"""

__version__ = "0.1.4"

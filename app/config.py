"""meterback 全局配置：单位换算、空值哨兵、阶段、Decimal 刻度。

规则唯一来源，其他模块只读：
- 用量 Decimal，单位 kWh，最小刻度 0.001；
- 金额 Decimal，单位元，最小刻度 0.01；
- 时间入库 UTC，月桶与展示按 Asia/Shanghai（+08:00）；
- 月桶左闭右开 [月初 00:00, 下月初 00:00)。
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHINA_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
UTC = dt.timezone.utc

RULE_VERSION = "meterback/1.0.0"

USAGE_QUANT = Decimal("0.001")
MONEY_QUANT = Decimal("0.01")
# 满量翻转判定：上一条读数需达到满量的这个比例，否则按倒走隔离
ROLLOVER_MIN_RATIO = Decimal("0.9")

REGISTERS = ("TOTAL", "PEAK", "FLAT", "VALLEY")
REGISTER_LABELS = {"TOTAL": "总", "PEAK": "峰", "FLAT": "平", "VALLEY": "谷"}
TOU_REGISTERS = ("PEAK", "FLAT", "VALLEY")

# M8 无功寄存器：只进力率子系统，不进 M1–M7 的有功流水线。
# REACTIVE 是无功电能总（ kvarh ）；M1–M7 仍把它当未知寄存器隔离，互不影响。
REACTIVE_REGISTERS = ("REACTIVE",)
REACTIVE_REGISTER_LABELS = {"REACTIVE": "无功"}
# 无功抄表里常见的寄存器写法 → 规范寄存器
REACTIVE_REGISTER_ALIASES = {
    "reactive": "REACTIVE", "无功": "REACTIVE", "无功总": "REACTIVE",
    "无功电量": "REACTIVE", "kvarh": "REACTIVE", "rq": "REACTIVE",
}
# 无功计量单位 → kvarh 换算（有功表上的度/kWh 写法也按 1 处理，量纲以契约寄存器为准）
UNIT_TO_KVARH = {
    "kvarh": Decimal("1"), "千乏时": Decimal("1"), "乏时": Decimal("0.001"),
    "mvarh": Decimal("1000"), "兆乏时": Decimal("1000"),
}
# 力率调整电费（M8）规则参数
PF_RULE_NAME = "power-factor-adjust/1.0.0"
# 观察窗口覆盖率（按天，无功已覆盖天数 / 当月自然天数）达到该比例才允许用实测力率
PF_MIN_COVERAGE = Decimal("0.60")
# 覆盖率不足或完全没有无功证据时的合同兜底力率（对应国标 0.90 考核档）
PF_CONTRACT_FALLBACK = Decimal("0.90")
# 力率查表前的量化刻度（cosφ 保留两位，按国标分档表就近取档）
PF_QUANT = Decimal("0.01")
# 支持的考核标准力率档位（国标三档）
PF_STANDARDS = (Decimal("0.90"), Decimal("0.85"), Decimal("0.80"))
REACTIVE_FILE = "reactive_dirty.csv"
REACTIVE_FINAL_FILE = "reactive_final.csv"
PF_EVIDENCE_DIRNAME = "pf_evidence"
PF_RELEASE_DIRNAME = "pf_releases"

# 单位 → kWh 换算系数（读数先归一到契约单位，用量再折 kWh）
UNIT_TO_KWH = {
    "kwh": Decimal("1"), "度": Decimal("1"), "千瓦时": Decimal("1"),
    "mwh": Decimal("1000"), "兆瓦时": Decimal("1000"),
}

# 空值哨兵：集中定义；None / 0 / 空串三态分开
SENTINELS = frozenset(
    {"", "-", "--", "—", "——", "/", "n/a", "na", "null", "none", "nil",
     "未知", "无", "暂无", "待定", "空", "未抄"}
)

READ_TYPE_ALIASES = {
    "n": "N", "正常": "N", "实抄": "N",
    "e": "E", "估抄": "E", "估算": "E",
}

STAGES = ("ingest", "contract", "quality", "restore", "allocate", "reconcile", "export",
          "pf_adjust")
STAGE_LABELS = {
    "ingest": "采信",
    "contract": "契约",
    "quality": "质量",
    "restore": "还原",
    "allocate": "切分",
    "reconcile": "对账",
    "export": "导出",
    "pf_adjust": "力率调整",
}
STAGE_FLOW = "采信 → 契约 → 质量 → 还原 → 切分 → 对账 → 导出"

# 列契约：规范列名 → 可接受的别名（GBK 导出的表头是中文）
COLUMN_ALIASES = {
    "meter_id": ("meter_id", "表号", "电能表", "电表编号", "meter"),
    "register": ("register", "寄存器", "分量", "费率"),
    "read_at": ("read_at", "抄表时间", "抄见时间", "时间"),
    "reading": ("reading", "读数", "示数", "累计读数"),
    "unit": ("unit", "单位", "计量单位"),
    "read_type": ("read_type", "抄表类型", "类型", "方式"),
    "note": ("note", "备注", "说明"),
}
REQUIRED_COLUMNS = ("meter_id", "register", "read_at", "reading")

# 可演示样例（白名单，避免任意路径读取）
SAMPLES = {
    "readings_dirty.csv": "脏样例 · 13 类问题",
    "readings_clean.csv": "干净样例",
    "readings_gbk.csv": "GBK 中文表头",
    "readings_truncated.csv": "末行截断",
    "readings_empty.csv": "空文件",
    "readings_header_only.csv": "仅表头",
    "readings_blank.csv": "全空行",
    "readings_bad_header.csv": "表头缺列",
    REACTIVE_FILE: "无功抄表包 · 缺证月份（暂估）",
    REACTIVE_FINAL_FILE: "无功抄表包 · 补齐定案（追补）",
}
DEFAULT_SAMPLE = "readings_dirty.csv"
METERS_FILE = "meters.csv"
PRICES_FILE = "prices.csv"

# 单一制电价兜底（prices.csv 缺失时）
DEFAULT_PRICES = {"TOTAL": Decimal("0.65"), "PEAK": Decimal("1.05"),
                  "FLAT": Decimal("0.65"), "VALLEY": Decimal("0.32")}


@dataclass(frozen=True)
class Settings:
    db_path: str = str(PROJECT_ROOT / "var" / "meterback.sqlite")
    sample_dir: str = str(PROJECT_ROOT / "sample_data")
    rule_version: str = RULE_VERSION

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        return replace(
            cls(),
            db_path=os.getenv("METERBACK_DB") or base.db_path,
            sample_dir=os.getenv("METERBACK_SAMPLE_DIR") or base.sample_dir,
        )

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).resolve().parent

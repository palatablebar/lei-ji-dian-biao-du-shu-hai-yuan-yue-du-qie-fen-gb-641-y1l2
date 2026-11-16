# 数据定义 meterback

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 力率调整（M8）字段与口径

### 10.1 无功抄表包

- 复用同一套列契约与文本归一口径；`register` 只接受无功寄存器（`无功`/`REACTIVE`/`无功总`/`无功电量`/`kvarh` 等，映射见 `config.REACTIVE_REGISTER_ALIASES`）。
- 单位归一到 kvarh（`config.UNIT_TO_KVARH`）；单位列留空或写 kWh/度 按 1 处理，量纲以寄存器为准。
- 抄表包指纹 = 原始字节 sha256；`package_id = "pf-" + sha256(有功批次ID, 指纹)[:16]`，同包重跑幂等。
- 原始字节落证目录：`var/pf_evidence/<package_id>/<filename>`，只写不覆盖（内容冲突报 `E_PF_EVIDENCE_CONFLICT`）。

### 10.2 力率账单（pf_bills）关键字段

| 字段 | 含义 |
|---|---|
| `evidence_grade` | `OBSERVED` 实测 / `LOW_COVERAGE` 覆盖不足兜底 / `NO_EVIDENCE` 无证据兜底 |
| `status` | `FINAL` 定案 / `PROVISIONAL` 暂估（待追补）；旧版被取代记 `SUPERSEDED`（保留不删） |
| `pf_basis` | `observed` 采用窗口实测力率 / `contract` 采用合同兜底力率 |
| `coverage` | 无功覆盖天数 / 当月自然天数，4 位小数；门槛 `PF_MIN_COVERAGE=0.60` |
| `window_start/end` | 无功观察窗口起止（UTC），复核据此看采用的观察范围 |
| `active_kwh` / `window_active_kwh` | 整月有功 / 观察窗口内有功；实测 cosφ 只用后者配窗口无功 |
| `reactive_kvarh` | 观察窗口内无功电量；无证据为 NULL |
| `observed_pf` / `applied_pf` | 窗口实测 cosφ（保留 2 位）/ 实际用于查表的 cosφ |
| `adjust_rate_pct` | 国标分档调整率（%），来自 `m8_powerfactor/table.py` |
| `adjustment_yuan` / `total_yuan` | 力率调整电费（0.01 元）/ 基础电费+调整后合计 |
| `true_up_of` / `prior_adjustment_yuan` / `true_up_delta_yuan` | 追补引用的原暂估账单、原调整额、本次差额 |

### 10.3 计算口径

- `cosφ = P / √(P² + Q²)`，P、Q 同取观察窗口；结果量化到 0.01（`PF_QUANT`），半进位后查分档表。
- `力率调整电费 = 基础电费 × 调整率%`，四舍五入到 0.01 元；基础电费取自已落库的有功批次账单，M8 不重算 M1–M7。
- 月桶、按日权重、半开区间与 M4/M5 完全同构（复用 `weights` 模块）。
- 发布产物目录 `var/pf_releases/<package_id>-v<seq>/`，四份文件同版本；`current` 为生效版本软链。

### 10.4 M8 原因码

文件级：`E_PF_PACKAGE_EMPTY`、`E_PF_HEADER_INVALID`、`E_PF_NO_BILLS`、`E_PF_STANDARD_UNSUPPORTED`、`E_PF_RELEASE_TARGET`、`E_PF_EVIDENCE_CONFLICT`。
行级：`Q_PF_BAD_READING`、`Q_PF_BAD_TIME`、`Q_PF_UNKNOWN_METER`、`Q_PF_BAD_REGISTER`、`Q_PF_REVERSE_READING`、`Q_PF_DUPLICATE_CONFLICT`。
标记：`K_PF_OBSERVED`、`K_PF_LOW_COVERAGE`、`K_PF_NO_EVIDENCE`、`K_PF_PROVISIONAL`、`K_PF_FINAL`、`K_PF_TRUE_UP`、`K_PF_REPLAY`。

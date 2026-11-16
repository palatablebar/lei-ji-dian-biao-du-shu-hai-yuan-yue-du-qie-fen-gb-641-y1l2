# 缺证月份力率调整电费裁决规则（M8）

> 适用场景：工业户无功表与有功表抄见时点不一致，月底有功电量已出、部分无功区间缺失。
> 目标：**不拿合同力率一刀切、也不卡住出账**——先形成可发布的暂定力率电费，
> 无功补齐后再定案追补；复核能看清每一笔用的是哪段观察范围、还欠哪些证据。

## 1. 裁决总则

| 规则 | 说明 |
|---|---|
| 不统一兜底 | 有足够实测证据的月份必须用实测力率，只有证据不足/缺失才退回合同力率，避免考核电费偏离实际 |
| 不阻塞出账 | 证据不完整也先出 **暂估（PROVISIONAL）** 版本，可正常发布；不等到无功补齐 |
| 同源 P/Q | 实测力率只能用**同一观察窗口内**的有功 P 与无功 Q 计算，禁止整月 P 配窗口 Q |
| 留痕可追 | 每笔电费带观察窗口起止、覆盖率、实测/采用力率、依据与标记；坏行带原始行号进隔离区 |
| 旧版不改 | 追补只在新版本挂差额并引用原暂估账单，旧版本永久保留、不重写 |
| 同包幂等 | 同一抄表包（原始字节 sha256）重跑命中同一 `package_id`，不新增任何电费 |
| 证据不丢 | 原始抄表包先 WORM 落证再解析；同名证据内容冲突拒绝覆盖 |

## 2. 证据等级与出账口径

设当月自然天数为 D，无功区间在当月实际覆盖天数为 d，覆盖率 `coverage = d / D`。

| 等级 | 触发条件 | 采用力率 | 账单状态 | 标记 |
|---|---|---|---|---|
| `OBSERVED` | coverage ≥ `PF_MIN_COVERAGE`（默认 0.60） | 窗口实测 cosφ | 覆盖整月→`FINAL`，否则 `PROVISIONAL` | `K_PF_OBSERVED`（未覆盖整月再加 `K_PF_PROVISIONAL`） |
| `LOW_COVERAGE` | 0 < coverage < 门槛 | 合同力率 `PF_CONTRACT_FALLBACK`（默认 0.90） | `PROVISIONAL` | `K_PF_LOW_COVERAGE`、`K_PF_PROVISIONAL`，**实测值与窗口仍保留展示** |
| `NO_EVIDENCE` | 当月无任何无功证据 | 合同力率 | `PROVISIONAL` | `K_PF_NO_EVIDENCE`、`K_PF_PROVISIONAL` |

- 实测力率：`cosφ = P / sqrt(P² + Q²)`，P 取观察窗口内有功电量、Q 取窗口内无功电量，
  结果量化到 0.01 后按国标分档表查调整率。
- 调整电费：`力率调整电费 = 当月基础电费（不含力率调整）× 调整率%`，金额按四舍五入到 0.01 元。
- P ≤ 0（当月无有功）时 cosφ 无定义，走合同兜底，不产生除零。

## 3. 定案与追补

1. 无功读数补齐后，用完整抄表包独立跑一次评估，得到定案候选。
2. 仅对「原状态为暂估、且本次已能按实测定案（OBSERVED 且覆盖整月）」的月处理：
   - 调整额发生变化（差额非 0）→ 生成追补，`true_up_delta = 定案调整额 − 暂估调整额`，
     挂 `K_PF_TRUE_UP` 并通过 `true_up_of` 引用原暂估账单；
   - 调整额未变 → 只留定案版本，不产生追补；
   - 补齐包仍无证据的月 → **继续保持暂估**，不强行定案。
3. 原暂估包的账单一字不改，复核可同时看到两版与差额。

## 4. 发布与同版本约束

- 发布在 `var/pf_releases/<package_id>-v<seq>/` 下原子落盘四份产物：
  `pf_bills.csv`（正式电费）、`pf_windows.csv`（观察窗口）、
  `pf_reconcile.json`（对账）、`pf_manifest.json`（清单）。
- 先写同卷临时目录再 `os.replace` 原子切名；发布中断只残留临时目录，
  `current` 软链仍指旧版，**旧版继续生效**。
- 四份产物与发布清单共享同一版本号、同一输入指纹、同一规则版本
  （`RULE_VERSION` + `PF_RULE_NAME`），保证「正式结果、导出、对账」同版本。
- 导出只允许读取版本目录内白名单文件，禁止路径穿越。

## 5. 对账

- **力率调整电费合计**：逐笔调整电费之和 = 合计字段（0.01 元逐位相等）。
- **追补差额闭合**：每笔 `定案调整额 − 暂估调整额 = true_up_delta`，全部追补差额求和闭合。
- 对账不平的版本清单 `all_balanced=false`，不允许被当作平衡结果发布说明。

## 6. 不变量（测试守护）

- 同抄表包重跑：`package_id` 不变、账单行数与金额不变（`test_same_package_rerun_is_idempotent`）。
- 原始证据：落盘字节与输入一致；冲突拒绝覆盖（`test_raw_evidence_stored_worm` 等）。
- 原子发布：模拟中断后 `current` 仍指旧版、旧目录保留（`test_interrupted_publish_keeps_previous_version`）。
- 追补：仅非零差额挂账、缺证月保持暂估、旧版不变（`test_finalize_*`）。
- 同源 P/Q：低覆盖月窗口 P 严格小于整月 P（`test_observed_p_and_q_come_from_same_window`）。

## 7. 参数与国标档位

- 参数集中在 `app/config.py`：`PF_MIN_COVERAGE`、`PF_CONTRACT_FALLBACK`、`PF_QUANT`、
  `PF_STANDARDS`、`PF_RULE_NAME`。
- 调整率分档表在 `app/modules/m8_powerfactor/table.py`，覆盖国标
  0.90 / 0.85 / 0.80 三档；改表必须升 `PF_RULE_NAME` 并同步补测试，
  否则同一份无功证据会算出两版电费。

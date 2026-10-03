# StockLab 数据源清单（更新：2026-10-03）

> 本文档记录当前项目已接入的所有免费数据源、调用链路、复权口径、已知限制与验收状态。

---

## 1. 行情通道（按 fallback 优先级）

| 级别 | Source | 类名 | SOURCE_NAME | 适用链路 | 备注 |
|---|---|---|---|---|---|
| P0 | AkShare | `AkShareDataSource` | `akshare(东方财富)` | 价格 / PE-TTM | 主源，30 年历史 |
| P1 | BaoStock | `BaoStockDataSource` | `baostock(证券宝)` | 价格 / PE-TTM | 备用历史行情 |
| P2 | 腾讯财经 | `TencentDataSource` | `tencent(腾讯财经)` | 价格（近 3 年重采样）/ 实时 / 简称 | 直连 HTTP，极速 |
| P3 | **新浪财经** | `SinaDataSource` | `sina(新浪财经)` | 实时 / 月线（约 4 年重采样）/ 简称 | HTTPS + Referer + GBK，**无复权因子时退回不复权** |
| P4 | **通达信** | `TdxDataSource` | `tdx(通达信)` | 实时 / 月线（仅不复权） | `tdxdata==0.1.1`（2026-09 新协议），**无复权因子、无名称** |

> **名词约定**：SOURCE_NAME 为 `used_source_name` 落库/展示值，**已接入三源（P0-P2）保持原值不变**，新增两源沿用同风格命名。

---

## 2. 复权口径

| Source | "" (不复权) | "qfq" (前复权) | "hfq" (后复权) | 说明 |
|---|---|---|---|---|
| AkShare | ✓ | ✓ | ✓ | 百度/东财上游 |
| BaoStock | ✓ | ✓ | ✓ | 证券宝自带 |
| Tencent | ✓ | **仅 qfq（硬编码）** | ✗ | 近 3 年日线重采样 → 月线 |
| **Sina** | ✓ | ✓ | ✓ | quotes.sina.cn 日线 + hfq.js 因子，**qfq = f(d)/f(latest)** |
| **TDX** | ✓ | ✗ | ✗ | **无复权因子能力**，非空 adjust_type 直接返回空表 |

> **验收口径**：qfq 价格必须在除权日不跳空（由 Sina `hfq.js` 验证，与新浪 `qfq.js` 推导值 33/33 一致）。

---

## 3. 数据源能力矩阵

| 能力 | AkShare | BaoStock | Tencent | **Sina** | **TDX** |
|---|---|---|---|---|---|
| fetch_monthly_close_prices | ✓ | ✓ | ✓（重采样） | ✓（重采样，≤4 年） | ✓（category=6，不复权） |
| fetch_monthly_pe_ttm | ✓ | ✓ | ✗ | ✗ | ✗ |
| fetch_stock_name | ✓ | ✓ | ✓ | ✓ | ✗（返回空串） |
| fetch_realtime_quote | ✗ | ✗ | ✓ | ✓ | ✓ |
| 复权因子 | 内置 | 内置 | 仅 qfq | hfq.js | 无 |

---

## 4. 实时行情字段完整度

| 字段 | Tencent | **Sina** | **TDX** |
|---|---|---|---|
| current_price | ✓ | ✓ | ✓ |
| yesterday_close | ✓ | ✓ | ✓ |
| today_open / high / low | ✓ | ✓ | ✓ |
| volume_shares | ✓ | ✓ | ✓（手→股 ×100） |
| amount_yuan | ✓ | ✓ | ✓ |
| change_amount / change_percent | ✓ | 自算 | 自算 |
| quote_time | ✓ | ✓ | ✗（空） |
| turnover_rate | ✓ | ✗（0） | ✗（0） |
| pe_ttm / pb_ratio | ✓ | ✗（None） | ✗（None） |
| total_mv / circulating_mv | ✓ | ✗（0） | ✗（0） |
| stock_name | ✓ | ✓ | ✗（空） |

> **降级链**：Tencent → Sina → TDX。前两级提供全字段，最后一级仅核心价格/量额。

---

## 5. 公告索引（巨潮资讯 CNINFO）

| 项目 | 说明 |
|---|---|
| 接口 | `POST http://www.cninfo.com.cn/new/hisAnnouncement/query` |
| 分页 | pageSize 硬限 30，`hasMore` / 本页<30 / `totalAnnouncement` 三重终止 |
| orgId | **禁止自拼**，走 `/szse_stock.json`（6259 条映射）或 `/topSearch/query` 精确匹配（代码完全一致） |
| 日期 | `announcementTime` 毫秒时间戳 **按 UTC+8 换算** → `announcement_date` |
| PDF | `http://static.cninfo.com.cn/` + `adjunctUrl` |
| category | `announcementTypeName` 常为 null → 允许 NULL |
| column 路由 | 6xx → sse，其余（含北交所） → szse；**bse 列查北交所返回 0** |
| 去重 | 1) `announcement_id` 主键；2) `(ts_code, announcement_date, title)` 二次去重（跨 id 同内容） |
| B 股 | 公告挂在 A 股代码下，`stock` 参数不接受 B 股代码 |

---

## 6. 同步阶段（`app/scripts/sync_market_data.py`）

| 阶段 | 子命令 | 默认全跑 | 说明 |
|---|---|---|---|
| 一 | securities | ✓ | 证券基础信息 |
| 二 | prices | ✓ | 日 K 行情（增量/全量） |
| 三 | valuations | ✓ | 最新估值快照 |
| 四 | valuation-history | ✗ | 历史估值序列（耗时最长） |
| 五 | indexes | ✓ | 宽基指数成分 |
| 六 | industries | ✗ | 行业估值横截面 |
| 七 | lifecycle | ✓ | 生命周期（上市/退市/事件） |
| 八 | fundamentals | ✗ | Point-in-Time 基本面（逐只） |
| **九** | **announcements** | **✗** | **巨潮公告索引（增量 / 全市场 / 逐只）** |

> **阶段九增量策略**：无 `--start-date` 时取 `MAX(announcement_date)` 作为起点；表为空时**拒绝执行**，必须显式给起始日期（如 `2015-01-01`），禁止默认无限回溯。

---

## 7. 已知限制与风控

| 源 | 限制 | 规避措施 |
|---|---|---|
| **新浪** | 1. 必须 HTTPS + Referer（缺失 403）<br>2. GBK 解码<br>3. 历史日线 ≤1023 根（≈4 年）<br>4. 大量抓取封 IP | 单线程串行、固定 0.8s 间隔、仅作为兜底 |
| **TDX (tdxdata)** | 1. 单请求 1-3s 固定延迟<br>2. 单出口并发 >60 触发限流<br>3. 无复权因子、无名称<br>4. 老 pytdx/mootdx 协议已被服务端拒绝（2026-09-10） | 短连接、单连接、仅最后兜底、调整类型非空直接拒绝 |
| **CNINFO** | 1. pageSize=30 硬限<br>2. 必须 orgId（精确匹配，模糊命中拒绝）<br>3. 无 stock 参数 = 全市场 | 模块级缓存 orgId、翻页 sleep、单只缺 orgId 直接跳过 |

---

## 8. 验收状态（免费数据源扩展第一阶段）

| 项 | 状态 | 备注 |
|---|---|---|
| ✓ Sina 实时行情（HTTPS+Referer+GBK） | 通过 | 600519 价格 1258.62 |
| ✓ Sina 月线重采样 + qfq/hfq | 通过 | 连续性验证（除权日无跳空） |
| ✓ TDX 连通（tdxdata 新协议） | 通过 | category=6 月线、market=2 北交所、价格与新浪一致 1258.62 |
| ✓ CNINFO orgId（全市场映射 + topSearch 精确） | 通过 | 920000 模糊命中 832000 被拒 |
| ✓ CNINFO 分页 / UTC+8 / PDF / 去重 | 通过 | 30 条/页、hasMore、title 清洗、静态站 PDF |
| ✓ quote_service 五级降级 | 通过 | 月线：AK→BS→TX→SN→TDX；实时：TX→SN→TDX |
| ✓ 阶段九 announcements 同步 | 通过 | 水位、全市场过滤、二次去重、--ts-code 逐只 |
| ✓ 离线测试 13/13 通过 | 通过 | 新增 4 个测试 + 原有 9 个回归 |
| ✓ live_check_sources.py 实测通过 | 通过 | 交叉校验新浪 1258.62 == TDX 1258.62 |

---

## 9. 依赖清单（requirements.txt 新增）

```
tdxdata==0.1.1  # 通达信 2026-09 新协议（Python >=3.11）
```

> **未新增**：sinadata、pytdx3（GitHub only，未发 PyPI）、mootdx、easy-tdx、eltdx（作为 TDX 备选保留在备忘）。

---

## 10. 测试矩阵

| 测试文件 | 类型 | 覆盖 |
|---|---|---|
| `test_sina_source.py` | 离线 | 符号转换 / JSONP / 实时 / 简称 / 月线复权 / 8 组异常 |
| `test_tdx_source.py` | 离线 | 市场编号 / 月线 category / 实时量额 / 6 组失败 / 简称空串 |
| `test_cninfo_client.py` | 离线 | 解析 / 分页 / orgId缓存+精确匹配 / 去重 / 5 组网络异常 |
| `test_announcements.py` | 离线 | 迁移005/Repo/同步水位/过滤/去重/逐只/幂等 |
| `live_check_sources.py` | 手工实网 | 实时/月线/公告/交叉校验（不进 CI） |
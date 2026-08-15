# 指数投资工作台 - 实现计划（v2，基于 MCP 实测数据）

## 概述

为指数投资者搭建云端自动化数据管道：从东方财富获取沪深300全收益指数（H00300.CSI）的历史行情与估值数据，以CSV存储在GitHub仓库，固化成纯Python脚本做周级增量更新，并通过GitHub Actions免费托管定时任务。架构可扩展，后续可新增其他指数。

## MCP 实测数据发现（已完成探测）

### H00300 全收益指数 — MCP 可用
- **实测结果**：`mx_index_block_finance_data` 查询 `"沪深300全收益指数H00300..."` 成功返回数据
- **有数据的字段**：收盘价、市盈率PE(TTM)、市净率PB
- **无数据的字段**：成交量、成交额、股息率（查询返回空 `{"data":[]}`）
- **日线数据起始**：至少从2010年有日线（2010年1月查询返回20个交易日）；2005年查询仅返回月线
- **单次查询上限**：一年日线数据（约242条）可一次返回

### 000300.SH 价格指数 — MCP 可用
- **有数据的字段**：收盘价、成交量、成交额、市盈率PE(TTM)、市净率PB
- **无数据的字段**：股息率（查询返回空）
- **日线数据起始**：2005-01-04（查询2005全年返回242个交易日）
- **关键发现**：000300.SH 与 399300.SZ 返回的成交量/成交额/PE/PB 完全一致（同一指数跨交易所）

### 数据合并策略
- **收盘价**：取 H00300（全收益指数，含分红再投资，如2026-08-14 收盘 6988.12 vs 价格指数 4665.88）
- **成交量/成交额**：取 000300.SH（H00300 无此数据）
- **PE(TTM)/PB**：两指数值相同（同一成分股池），取 H00300 即可
- **股息率**：eastmoney MCP 不提供，计划中标注为"暂不可用"

### MCP 返回格式（关键）
MCP 返回透视表格式：`columns` 为日期数组（最新在前），`items` 为指标行数组。需转置为 CSV 行格式（每行一个日期）。
```json
{
  "columns": ["沪深300全收益(H00300.CSI)(指数)", "2026-08-14(日)", "2026-08-13(日)", ...],
  "items": [
    ["市净率PB", "1.44倍", "1.442倍", ...],
    ["市盈率PE(TTM)", "14.31倍", "14.32倍", ...],
    ["收盘价", "6988.1173点", "6984.8466点", ...]
  ]
}
```
数值需解析单位：`倍`→float、`点`→float、`亿股`→float、`亿`→float。

### GitHub 认证用户
- `login`: `ShawnLee42`
- 仓库将创建在此账户下

## 仓库结构

```
index-investment-workbench/
├── README.md                          # 项目说明、数据字典、使用方法
├── .github/workflows/weekly-update.yml # 周级自动更新 workflow
├── scripts/update_index.py            # PEP 723 内联依赖脚本（增量更新）
├── config/indices.json                # 指数配置（可扩展）
└── data/h00300/                       # 指数代码目录（用全收益代码）
    ├── meta.json                      # 指数元数据
    └── daily.csv                      # 日级数据（价格+估值）
```

### config/indices.json
```json
{
  "indices": [
    {
      "code": "h00300",
      "name": "沪深300全收益",
      "mcp_code": "H00300.CSI",
      "price_index_code": "000300.SH",
      "price_index_secid": "1.000300",
      "data_dir": "data/h00300",
      "start_date": "20100101",
      "enabled": true
    }
  ]
}
```
后续新增指数只需在此文件追加条目。

### daily.csv 表头
```csv
date,close,volume,amount,pe_ttm,pb,dividend_yield
```
| 列名 | 来源 | 说明 |
|---|---|---|
| date | H00300 查询 | 交易日期 YYYY-MM-DD |
| close | H00300 查询 | 全收益收盘价 |
| volume | 000300.SH 查询 | 成交量（亿股） |
| amount | 000300.SH 查询 | 成交额（亿元） |
| pe_ttm | H00300 查询 | 市盈率TTM（倍） |
| pb | H00300 查询 | 市净率（倍） |
| dividend_yield | 暂不可用 | 留空，后续寻找数据源 |

## 实施步骤

### 步骤1：MCP 数据初始化 + 仓库创建

**1.1 创建仓库**
- 调用 `create_repository`：
  - `name`: `"index-investment-workbench"`
  - `description`: `"指数投资工作台 - 沪深300等指数历史行情与估值数据"`
  - `autoInit`: `true`
  - `private`: `false`

**1.2 获取 H00300 全收益历史数据（收盘价+PE+PB）**
- 按年分批调用 `mx_index_block_finance_data`，每批查一年：
  - `"沪深300全收益指数H00300从2010年1月1日至2010年12月31日每日的收盘价、市盈率PE(TTM)、市净率PB"`
  - `"沪深300全收益指数H00300从2011年1月1日至2011年12月31日每日的收盘价、市盈率PE(TTM)、市净率PB"`
  - ... 依此类推至2026年
- 预计约17次 MCP 调用（2010-2026），每次返回约242条日线

**1.3 获取 000300.SH 量价数据（成交量+成交额）**
- 按年分批调用 `mx_index_block_finance_data`：
  - `"沪深300指数000300从2010年1月1日至2010年12月31日每日的成交量和成交额"`
  - ... 与H00300同年份范围对齐
- 预计约17次 MCP 调用

**1.4 数据合并与转换**
- 解析 MCP 透视表格式：`columns`（日期数组）× `items`（指标行）→ 转置为行格式
- 单位解析：`1.44倍`→`1.44`，`6988.1173点`→`6988.1173`，`178.4亿股`→`178.4`，`5498亿`→`5498`
- 按 date 合并 H00300（close/pe_ttm/pb）和 000300（volume/amount）
- 按日期升序排列，去除重复日期
- 生成 `data/h00300/daily.csv`

**1.5 生成所有文件并推送**
- 生成 `config/indices.json`、`data/h00300/meta.json`、`README.md`、`.gitignore`
- 生成 `scripts/update_index.py`（步骤2脚本）
- 生成 `.github/workflows/weekly-update.yml`（步骤3 workflow）
- 调用 `push_files` 一次性推送所有文件到 `main` 分支

**1.6 验证**
- 调用 `get_file_contents` 检查文件结构
- 检查 CSV 行数（2010-2026约4000+交易日）

### 步骤2：纯 Python 增量更新脚本（摸清套路 → 固化）

**核心思路**：MCP 只在 LLM 会话中可用。固化脚本需直接调东方财富 HTTP API，不走 MCP。

**"摸清套路"过程**：
1. 步骤1中通过 MCP 已确认：H00300 有 close/PE/PB，000300 有 volume/amount
2. 脚本中用 push2his K线 API 获取 000300 的量价数据（secid=1.000300），该 API 返回 open/high/low/close/volume/amount
3. 对于 H00300 的 close（全收益价格）和 PE/PB：
   - push2his K线 API 可能支持 H00300 的 secid（需在脚本中探测 `1.H00300` 或 `2.H00300`）
   - PE/PB 不在 K线 API 的标准返回字段中，需通过以下策略获取：
     - 策略A：push2his API 的 `fields1` 参数可能包含估值字段（f162=PE, f164=PB）
     - 策略B：datacenter API `https://datacenter-web.eastmoney.com/api/data/v1/get` 查询指数估值
     - 策略C：push2 实时 API `http://push2.eastmoney.com/api/qt/stock/get` 获取当日估值（仅最新值）
   - 脚本中按策略A→B→C降级尝试，并在日志中输出哪个策略成功

**文件**：`scripts/update_index.py`，PEP 723 内联依赖

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
```

**核心逻辑**：
1. 读取 `config/indices.json` 获取所有启用指数
2. 对每个指数：
   - 读取 `data/{code}/daily.csv` 找最后日期
   - 计算次日作为查询起始日
   - 调 push2his K线 API 获取增量 close/volume/amount
   - 尝试获取增量 PE/PB（多策略降级）
   - 合并后追加写入 CSV
3. 支持首次全量初始化（CSV不存在时从 start_date 开始全量获取）

**关键函数**：
- `fetch_kline(secid, beg, end)` — push2his K线 API，解析逗号分隔的 klines 数组
- `fetch_valuation(secid, code, beg, end)` — 多策略获取PE/PB（push2his扩展字段→datacenter→push2实时）
- `parse_mcp_pivot_to_rows(data)` — 复用步骤1的透视表解析逻辑（仅初始化时用）
- `get_last_date(csv_path)` — 读CSV最后一行日期
- `append_rows(csv_path, rows, is_new)` — 追加写入，新文件写表头
- `update_index(cfg)` — 单指数更新主流程

**设计要点**：
- 零运行时大模型依赖：纯 HTTP API，不经过 MCP
- 优雅降级：估值获取失败时价格列仍正常写入，估值列留空
- 错误隔离：单个指数失败不影响其他
- 详细日志：输出每次API调用结果和降级策略

### 步骤3：GitHub Actions 周级定时更新

**文件**：`.github/workflows/weekly-update.yml`

```yaml
name: Weekly Index Update
on:
  schedule:
    - cron: '0 8 * * 6'   # UTC 周六08:00 = 北京时间周六16:00
  workflow_dispatch: {}
permissions:
  contents: write
jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          version: "latest"
      - name: Run update script
        run: uv run scripts/update_index.py
        env:
          TZ: Asia/Shanghai
      - name: Commit and push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/
          if git diff --staged --quiet; then
            git commit --allow-empty -m "Keepalive: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
          else
            git commit -m "Weekly data update: $(date -u '+%Y-%m-%d')"
          fi
          git push
```

**设计要点**：
- `permissions: contents: write` 允许 GITHUB_TOKEN 推送
- `astral-sh/setup-uv@v5` 自动安装 UV，`uv run` 自动解析 PEP 723 依赖
- 无数据变更时创建空 commit，规避60天自动禁用
- `workflow_dispatch` 允许手动触发测试

## 假设与决策

| 决策项 | 选择 | 理由 |
|---|---|---|
| 仓库名 | `index-investment-workbench` | GitHub 不允许中文仓库名，中文名放入 description |
| 仓库可见性 | 公开 | Actions 无分钟限制，数据可共享 |
| 收盘价来源 | H00300 全收益指数 | 用户明确要求全收益，MCP 已确认可用 |
| 成交量来源 | 000300.SH 价格指数 | H00300 无量价数据，两指数同一成分股池 |
| 数据起始日 | 2010-01-01 | H00300 日线数据最早确认到2010；2005年仅有月线 |
| 股息率 | 暂留空 | eastmoney MCP 不提供指数股息率，后续可考虑其他数据源 |
| 脚本依赖 | 仅 `requests` + 标准库 | 最小化依赖，UV PEP 723 兼容 |
| cron 时区 | UTC 08:00 周六 = 北京16:00 | 周六收盘后更新 |
| 估值降级策略 | push2his扩展字段→datacenter→push2实时→留空 | 保证价格数据始终可用 |

## 验证步骤

### 步骤1验证（MCP 初始化后）
- `get_file_contents` 检查仓库文件结构完整
- 检查 CSV 表头：`date,close,volume,amount,pe_ttm,pb,dividend_yield`
- 检查 CSV 行数：2010-2026约4000+交易日
- 检查 close 列为全收益价格（约6000-7000点区间，非价格指数的4000-5000点）
- 检查 volume/amount 列有值（来自000300.SH）
- 检查 pe_ttm/pb 列有值（来自H00300）

### 步骤2验证（Python 脚本）
- 在 workspace 本地运行 `uv run scripts/update_index.py --index h00300`
- 二次运行：检查增量逻辑（新增0条或少量条目）
- 检查 K线数据合理性
- 检查估值列有值或优雅留空
- 检查日志中输出哪个估值获取策略成功

### 步骤3验证（GitHub Actions）
- GitHub Actions 页面手动触发 workflow
- 检查日志：UV安装成功、脚本无报错、commit/push成功
- 运行后检查仓库 CSV 是否更新
- 非交易日运行检查空 commit 是否创建

## 可扩展性

新增指数（如中证500全收益 H00905）只需：
1. 在 `config/indices.json` 追加配置
2. 运行 `uv run scripts/update_index.py --index h00905` 初始化历史数据
3. 后续 weekly-update workflow 自动包含新指数

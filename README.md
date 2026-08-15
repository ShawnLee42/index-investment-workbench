# 指数投资工作台

自动化获取并维护 A 股指数的历史行情与估值数据，数据存储为 CSV，通过 GitHub Actions 每周自动更新。

## 数据来源

所有数据来自 [东方财富](https://www.eastmoney.com/) 公开 HTTP API，无需 API Key。

| API 端点 | 用途 |
|---|---|
| `push2his.eastmoney.com/api/qt/stock/kline/get` | K线行情 (收盘价/成交量/成交额) |
| `datacenter-web.eastmoney.com/api/data/v1/get` | 指数估值 (PE/PB) |
| `push2.eastmoney.com/api/qt/stock/get` | 实时行情兜底 |

> **设计说明**: 本项目的数据获取逻辑最初通过 Trae MCP (东方财富插件) 探索验证，
> 随后将 MCP 的调用模式固化为纯 Python HTTP 请求，完全脱离 MCP 运行。
> 这样脚本可以在任何有网络的环境执行，包括 GitHub Actions。

## 数据字典

### data/{index_code}/daily.csv

| 列名 | 类型 | 单位 | 说明 |
|---|---|---|---|
| date | string | - | 交易日期 YYYY-MM-DD |
| close | float | 点 | 收盘价 (来自价格指数) |
| volume | float | 手 | 成交量 |
| amount | float | 元 | 成交额 |
| pe_ttm | float | 倍 | 市盈率 (TTM) |
| pb | float | 倍 | 市净率 |
| dividend_yield | float | % | 股息率 (暂不可用，留空) |

## 已收录指数

| 代码 | 名称 | 起始日期 |
|---|---|---|
| h00300 | 沪深300全收益 | 2010-01-01 |

## 使用方法

### 本地运行

```bash
# 安装 uv (Python 包管理器)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 增量更新所有指数
uv run scripts/update_index.py

# 全量重新获取
uv run scripts/update_index.py --full

# 只更新指定指数
uv run scripts/update_index.py --index h00300
```

### 自动更新 (GitHub Actions)

仓库已配置 `.github/workflows/weekly-update.yml`，每周六 16:00 (北京时间) 自动运行增量更新。
也可在 GitHub 仓库的 Actions 页面手动触发 (workflow_dispatch)。

## 新增指数

在 `config/indices.json` 中添加条目:

```json
{
  "code": "000905",
  "name": "中证500",
  "mcp_code": "000905.SH",
  "price_index_code": "000905",
  "price_index_secid": "1.000905",
  "data_dir": "data/000905",
  "start_date": "20100101",
  "enabled": true
}
```

然后运行 `uv run scripts/update_index.py --index 000905` 即可初始化数据。

## 仓库结构

```
index-investment-workbench/
├── README.md
├── .github/workflows/weekly-update.yml
├── scripts/update_index.py     # PEP 723 内联依赖脚本
├── config/indices.json         # 指数配置 (可扩展)
└── data/
    └── h00300/
        ├── meta.json           # 指数元数据 (自动更新)
        └── daily.csv           # 日级数据
```

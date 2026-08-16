# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""
指数投资工作台 - 数据更新脚本

直接调用东方财富公开 HTTP API，不依赖 MCP/Skill。
支持全量初始化和增量更新。

数据来源:
  - K线行情 (收盘价/成交量/成交额): push2his.eastmoney.com
  - 指数估值 (PE/PB): datacenter-web.eastmoney.com
  - 实时行情兜底: push2his.eastmoney.com (最新日K线)

用法:
    uv run scripts/update_index.py                # 增量更新所有启用指数
    uv run scripts/update_index.py --full          # 全量重新获取
    uv run scripts/update_index.py --index h00300  # 只更新指定指数
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── 东方财富 API 端点 ──────────────────────────────────────────
API_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
API_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
API_SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

# K线字段: f51=日期, f52=开盘, f53=收盘, f54=最高, f55=最低,
#          f56=成交量(手), f57=成交额(元), f58=振幅
KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58"

# 项目根目录 (脚本在 scripts/ 下，根目录是上一级)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "indices.json"


# ── HTTP 请求封装 ──────────────────────────────────────────────
def http_get(url, params, timeout=30, retries=3):
    """带重试的 GET 请求。"""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"  ⚠ 请求失败 (第{attempt+1}次): {e}, {wait}秒后重试...")
                time.sleep(wait)
            else:
                print(f"  ⚠ 请求最终失败: {url}")
                return None
    return None


# ── secid 查找 ─────────────────────────────────────────────────
def find_secid(code):
    """通过东方财富搜索 API 查找指数的 secid。"""
    preset = {
        "000300": "1.000300",
        "000905": "1.000905",
        "000016": "1.000016",
        "000852": "1.000852",
        "399006": "0.399006",
        "399300": "0.399300",
    }
    if code in preset:
        return preset[code]
    try:
        data = http_get(API_SEARCH, {
            "input": code, "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 5,
        })
        if data and data.get("QuotationCodeTable"):
            for item in data["QuotationCodeTable"]:
                if item.get("Code") == code.upper():
                    return f"{item.get('MktNum', '1')}.{code}"
    except Exception:
        pass
    return f"1.{code}"


# ── K线数据获取 ────────────────────────────────────────────────
def fetch_kline(secid, beg_date, end_date, klt=101):
    """从 push2his API 获取 K 线数据。"""
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": KLINE_FIELDS,
        "klt": str(klt),
        "fqt": "0",
        "beg": beg_date,
        "end": end_date,
        "lmt": "1000000",
    }
    data = http_get(API_KLINE, params)
    if not data or not data.get("data") or not data["data"].get("klines"):
        print(f"  ⚠ K线数据为空: secid={secid}, {beg_date}~{end_date}")
        return []

    result = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            result.append({
                "date": parts[0],
                "close": _safe_float(parts[2]),
                "volume": _safe_float(parts[5]),
                "amount": _safe_float(parts[6]),
            })
        except (ValueError, IndexError):
            continue
    return result


# ── 指数估值数据获取 (PE/PB) ───────────────────────────────────
def fetch_valuation(secucode, beg_date, end_date):
    """
    从 datacenter API 获取指数历史估值数据 (PE/PB)。
    自动分页获取全部数据。
    """
    beg = f"{beg_date[:4]}-{beg_date[4:6]}-{beg_date[6:8]}"
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    # 尝试多个 reportName + filter 组合
    configs = [
        # 标准指数估值报告
        {"reportName": "RPT_INDEX_VALUATIONANALYSIS",
         "filter": f'(SECUCODE="{secucode}")(TRADE_DATE>=\'{beg}\')(TRADE_DATE<=\'{end}\')'},
        # 备选: 用 SECURITY_CODE 过滤
        {"reportName": "RPT_INDEX_VALUATIONANALYSIS",
         "filter": f'(SECURITY_CODE="{secucode.split(".")[0]}")(TRADE_DATE>=\'{beg}\')(TRADE_DATE<=\'{end}\')'},
        # 备选报告名
        {"reportName": "RPT_INDEX_TS",
         "filter": f'(SECUCODE="{secucode}")(TRADE_DATE>=\'{beg}\')(TRADE_DATE<=\'{end}\')'},
        # 备选: 指数基本信息服务
        {"reportName": "RPT_INDEX_BASICINFO",
         "filter": f'(SECUCODE="{secucode}")'},
    ]

    for i, cfg in enumerate(configs):
        all_rows = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            params = {
                "reportName": cfg["reportName"],
                "columns": "ALL",
                "filter": cfg["filter"],
                "pageNumber": str(page),
                "pageSize": "500",
                "sortTypes": "-1",
                "sortColumns": "TRADE_DATE",
            }
            data = http_get(API_DATACENTER, params)
            if not data or not data.get("result"):
                break

            result = data["result"]
            rows = result.get("data", [])
            if not rows:
                break

            all_rows.extend(rows)
            total_count = result.get("totalCount", len(all_rows))
            total_pages = math.ceil(total_count / 500) if total_count > 0 else 1
            page += 1

        if all_rows:
            print(f"  ✓ 估值数据获取成功: {cfg['reportName']} (配置{i+1}), {len(all_rows)} 条")
            return _parse_valuation(all_rows)

    print(f"  ⚠ 估值数据获取失败 (所有配置均未返回数据)")
    return []


def _parse_valuation(rows):
    """解析 datacenter API 返回的估值数据，统一字段名。"""
    result = []
    for row in rows:
        trade_date = row.get("TRADE_DATE") or row.get("DATE") or ""
        trade_date = trade_date[:10] if trade_date else ""

        pe = (row.get("PE_TTM") or row.get("PE") or
              row.get("VAL_PE_TTM") or row.get("INDEX_PE") or
              row.get("PE_TTM2"))
        pb = (row.get("PB") or row.get("VAL_PB") or row.get("INDEX_PB") or
              row.get("PB_NEW"))
        dv = (row.get("DV_TTM") or row.get("DIVIDEND_YIELD") or
              row.get("DV_RATIO") or row.get("DIVIDEND_YIELD_TTM"))

        result.append({
            "date": trade_date,
            "pe_ttm": _safe_float(pe),
            "pb": _safe_float(pb),
            "dividend_yield": _safe_float(dv),
        })
    return result


# ── 实时行情兜底 (用最新日K线获取PE/PB) ────────────────────────
def fetch_latest_valuation(secid):
    """
    用 push2his API 获取最近5个交易日的K线，
    取最新一天的数据作为兜底估值。
    """
    today = datetime.now().strftime("%Y%m%d")
    beg = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")

    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f161,f162,f163,f173,f184,f185",
        "klt": "101",
        "fqt": "0",
        "beg": beg,
        "end": today,
        "lmt": "5",
    }
    try:
        data = http_get(API_KLINE, params)
        if data and data.get("data") and data["data"].get("klines"):
            klines = data["data"]["klines"]
            if klines:
                parts = klines[-1].split(",")
                # f162=PE(动态), f163=PB, f173=PE(TTM)
                # 字段索引: 0=date, ..., 13=f173, 14=f184, 15=f185
                pe_ttm = _safe_float(parts[13]) if len(parts) > 13 else None  # f173
                pb = _safe_float(parts[11]) if len(parts) > 11 else None     # f163
                if pe_ttm or pb:
                    return {"pe_ttm": pe_ttm, "pb": pb}
    except Exception:
        pass
    return None


# ── 数据合并 ───────────────────────────────────────────────────
def merge_data(kline_rows, valuation_rows, realtime=None):
    """合并 K线数据和估值数据，按 date 合并。"""
    val_map = {}
    for v in valuation_rows:
        if v["date"]:
            val_map[v["date"]] = v

    merged = []
    for k in kline_rows:
        row = {
            "date": k["date"],
            "close": k["close"],
            "volume": k["volume"],
            "amount": k["amount"],
            "pe_ttm": "",
            "pb": "",
            "dividend_yield": "",
        }
        v = val_map.get(k["date"])
        if v:
            row["pe_ttm"] = v["pe_ttm"] or ""
            row["pb"] = v["pb"] or ""
            row["dividend_yield"] = v["dividend_yield"] or ""
        merged.append(row)

    # 如果有实时数据且最后一天没有估值，补充
    if realtime and merged:
        last = merged[-1]
        if not last["pe_ttm"] and realtime.get("pe_ttm"):
            last["pe_ttm"] = realtime["pe_ttm"]
        if not last["pb"] and realtime.get("pb"):
            last["pb"] = realtime["pb"]

    return merged


# ── CSV 读写 ───────────────────────────────────────────────────
CSV_COLUMNS = ["date", "close", "volume", "amount", "pe_ttm", "pb", "dividend_yield"]


def get_last_date(csv_path):
    if not csv_path.exists():
        return None
    last_date = None
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("date", "").strip()
            if d:
                last_date = d
    return last_date


def read_existing_dates(csv_path):
    dates = set()
    if not csv_path.exists():
        return dates
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("date", "").strip()
            if d:
                dates.add(d)
    return dates


def write_csv(csv_path, rows):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  ✓ 写入 {len(rows)} 条记录到 {csv_path.name}")


def append_csv(csv_path, new_rows):
    existing = read_existing_dates(csv_path)
    truly_new = [r for r in new_rows if r["date"] not in existing]
    if not truly_new:
        print(f"  ✓ 无新数据需要追加")
        return 0
    file_exists = csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
            csv_path.parent.mkdir(parents=True, exist_ok=True)
        writer.writerows(truly_new)
    print(f"  ✓ 追加 {len(truly_new)} 条新记录到 {csv_path.name}")
    return len(truly_new)


# ── 单指数更新 ─────────────────────────────────────────────────
def update_index(cfg, force_full=False):
    code = cfg["code"]
    name = cfg.get("name", code)
    data_dir = ROOT / cfg.get("data_dir", f"data/{code}")
    csv_path = data_dir / "daily.csv"
    start_date = cfg.get("start_date", "20100101")
    price_secid = cfg.get("price_index_secid", find_secid(cfg.get("price_index_code", code)))
    # 估值查询用的 secucode: 优先用 valuation_secucode，其次用 price_index_code.SH
    valuation_secucode = cfg.get("valuation_secucode",
                                  f"{cfg.get('price_index_code', code)}.SH")

    today = datetime.now().strftime("%Y%m%d")
    today_fmt = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"更新指数: {name} ({code})")
    print(f"  secid: {price_secid}")
    print(f"  valuation_secucode: {valuation_secucode}")
    print(f"  数据目录: {data_dir}")

    # 确定获取范围
    last_date = get_last_date(csv_path) if not force_full else None

    if last_date:
        last_dt = datetime.strptime(last_date, "%Y-%m-%d")
        beg = (last_dt + timedelta(days=1)).strftime("%Y%m%d")
        print(f"  模式: 增量更新 ({last_date} -> {today_fmt})")
    else:
        beg = start_date
        print(f"  模式: 全量初始化 ({start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} -> {today_fmt})")

    # 1. 获取 K线数据 (收盘价/成交量/成交额)
    print(f"  获取K线数据: {price_secid}, {beg}~{today}")
    kline_rows = fetch_kline(price_secid, beg, today)
    if not kline_rows:
        if last_date:
            print(f"  ✓ 无新交易数据 (上次更新: {last_date})")
            return True  # 增量更新无新数据不算失败
        print(f"  ✗ K线数据获取失败，跳过")
        return False
    print(f"  ✓ K线数据: {len(kline_rows)} 条")

    # 2. 获取估值数据 (PE/PB) - 使用 valuation_secucode
    print(f"  获取估值数据: {valuation_secucode}, {beg}~{today}")
    val_rows = fetch_valuation(valuation_secucode, beg, today)

    # 3. 获取实时估值兜底
    realtime = None
    if kline_rows:
        realtime = fetch_latest_valuation(price_secid)
        if realtime:
            print(f"  ✓ 最新估值兜底: PE={realtime.get('pe_ttm')}, PB={realtime.get('pb')}")

    # 4. 合并数据
    merged = merge_data(kline_rows, val_rows, realtime)
    if not merged:
        print(f"  ✗ 合并后无数据，跳过")
        return False

    # 统计估值覆盖率
    pe_count = sum(1 for r in merged if r["pe_ttm"])
    pb_count = sum(1 for r in merged if r["pb"])
    print(f"  估值覆盖: PE={pe_count}/{len(merged)}, PB={pb_count}/{len(merged)}")

    # 5. 写入 CSV
    if force_full or not csv_path.exists():
        write_csv(csv_path, merged)
    else:
        append_csv(csv_path, merged)

    # 6. 更新 meta.json
    meta = {
        "code": code,
        "name": name,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_records": len(read_existing_dates(csv_path)),
        "last_date": get_last_date(csv_path),
        "data_source": "eastmoney",
    }
    meta_path = data_dir / "meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"  ✓ 元数据已更新: {meta_path.name}")

    return True


# ── 工具函数 ───────────────────────────────────────────────────
def _safe_float(val):
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ── 主函数 ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="指数数据更新工具")
    parser.add_argument("--full", action="store_true", help="全量重新获取")
    parser.add_argument("--index", type=str, help="只更新指定指数代码")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"✗ 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    indices = config.get("indices", [])
    if args.index:
        indices = [i for i in indices if i["code"] == args.index]
        if not indices:
            print(f"✗ 未找到指数: {args.index}")
            sys.exit(1)

    enabled = [i for i in indices if i.get("enabled", True)]
    print(f"共 {len(enabled)} 个指数需要更新")

    success = 0
    failed = 0
    for cfg in enabled:
        try:
            if update_index(cfg, force_full=args.full):
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ✗ 更新失败: {cfg['code']} - {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"更新完成: 成功 {success}, 失败 {failed}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

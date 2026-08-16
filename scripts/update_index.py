# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""
指数投资工作台 - 数据更新脚本

直接调用东方财富公开 HTTP API，不依赖 MCP/Skill。
所有数据（收盘价/成交量/成交额/PE/PB）均从 push2his K线API 获取。

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
#          f56=成交量(手), f57=成交额(元), f58=振幅,
#          f59=涨跌幅, f60=涨跌额, f161=换手率,
#          f162=PE(动态), f163=PB, f173=PE(TTM)
KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f161,f162,f163,f173"

# 字段索引映射 (对应 KLINE_FIELDS 中的顺序)
# 0=date, 1=open, 2=close, 3=high, 4=low, 5=volume, 6=amount,
# 7=amplitude, 8=change_pct, 9=change_amt, 10=turnover,
# 11=pe_dynamic, 12=pb, 13=pe_ttm
IDX_DATE = 0
IDX_CLOSE = 2
IDX_VOLUME = 5
IDX_AMOUNT = 6
IDX_PE_DYNAMIC = 11
IDX_PB = 12
IDX_PE_TTM = 13

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "indices.json"


# ── HTTP 请求封装 ──────────────────────────────────────────────
def http_get(url, params, timeout=30, retries=5):
    """带重试的 GET 请求。"""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                wait = 3 * (2 ** attempt)  # 3, 6, 12, 24 秒
                print(f"\n  ⚠ 请求失败 (第{attempt+1}次): {e}, {wait}秒后重试...", end="", flush=True)
                time.sleep(wait)
            else:
                print(f"\n  ⚠ 请求最终失败: {url}")
                return None
    return None


# ── secid 查找 ─────────────────────────────────────────────────
def find_secid(code):
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


# ── K线数据获取 (含PE/PB) ─────────────────────────────────────
def fetch_kline(secid, beg_date, end_date, klt=101):
    """从 push2his API 获取 K 线数据 (含 PE/PB)，按年分批。"""
    beg_year = int(beg_date[:4])
    end_year = int(end_date[:4])
    if end_year - beg_year > 1:
        return _fetch_kline_chunked(secid, beg_date, end_date, klt)
    return _fetch_kline_single(secid, beg_date, end_date, klt)


def _fetch_kline_single(secid, beg_date, end_date, klt=101):
    """单次 K 线请求。"""
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
        return []
    return _parse_klines(data["data"]["klines"])


def _fetch_kline_chunked(secid, beg_date, end_date, klt=101):
    """按年分批获取 K 线数据。"""
    beg_year = int(beg_date[:4])
    end_year = int(end_date[:4])
    all_rows = []

    for year in range(beg_year, end_year + 1):
        y_beg = beg_date if year == beg_year else f"{year}0101"
        y_end = end_date if year == end_year else f"{year}1231"

        print(f"    {year}...", end=" ", flush=True)
        rows = _fetch_kline_single(secid, y_beg, y_end, klt)
        print(f"{len(rows)} 条", flush=True)
        all_rows.extend(rows)
        time.sleep(2)  # 2秒间隔，避免限流

    if not all_rows:
        print(f"  ⚠ K线数据为空: secid={secid}, {beg_date}~{end_date}")
    return all_rows


def _parse_klines(klines):
    """解析 K线数据行，提取 close/volume/amount/pe_ttm/pb。"""
    result = []
    for line in klines:
        parts = line.split(",")
        if len(parts) <= IDX_CLOSE:
            continue
        try:
            row = {
                "date": parts[IDX_DATE],
                "close": _safe_float(parts[IDX_CLOSE]),
                "volume": _safe_float(parts[IDX_VOLUME]) if len(parts) > IDX_VOLUME else None,
                "amount": _safe_float(parts[IDX_AMOUNT]) if len(parts) > IDX_AMOUNT else None,
                "pe_ttm": _safe_float(parts[IDX_PE_TTM]) if len(parts) > IDX_PE_TTM else None,
                "pb": _safe_float(parts[IDX_PB]) if len(parts) > IDX_PB else None,
            }
            # 如果 TTM PE 为空，用动态 PE 兜底
            if not row["pe_ttm"] and len(parts) > IDX_PE_DYNAMIC:
                row["pe_ttm"] = _safe_float(parts[IDX_PE_DYNAMIC])
            result.append(row)
        except (ValueError, IndexError):
            continue
    return result


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

    today = datetime.now().strftime("%Y%m%d")
    today_fmt = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"更新指数: {name} ({code})")
    print(f"  secid: {price_secid}")
    print(f"  数据目录: {data_dir}")

    last_date = get_last_date(csv_path) if not force_full else None

    if last_date:
        last_dt = datetime.strptime(last_date, "%Y-%m-%d")
        beg = (last_dt + timedelta(days=1)).strftime("%Y%m%d")
        print(f"  模式: 增量更新 ({last_date} -> {today_fmt})")
    else:
        beg = start_date
        print(f"  模式: 全量初始化 ({start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} -> {today_fmt})")

    # 获取 K线数据 (含 PE/PB)
    print(f"  获取K线数据: {price_secid}, {beg}~{today}")
    kline_rows = fetch_kline(price_secid, beg, today)
    if not kline_rows:
        if last_date:
            print(f"  ✓ 无新交易数据 (上次更新: {last_date})")
            return True
        print(f"  ✗ K线数据获取失败")
        return False
    print(f"  ✓ K线数据: {len(kline_rows)} 条")

    # 转换为 CSV 行格式
    merged = []
    for k in kline_rows:
        merged.append({
            "date": k["date"],
            "close": k["close"] or "",
            "volume": k["volume"] or "",
            "amount": k["amount"] or "",
            "pe_ttm": k["pe_ttm"] or "",
            "pb": k["pb"] or "",
            "dividend_yield": "",
        })

    # 统计估值覆盖率
    pe_count = sum(1 for r in merged if r["pe_ttm"])
    pb_count = sum(1 for r in merged if r["pb"])
    print(f"  估值覆盖: PE={pe_count}/{len(merged)}, PB={pb_count}/{len(merged)}")

    # 写入 CSV
    if force_full or not csv_path.exists():
        write_csv(csv_path, merged)
    else:
        append_csv(csv_path, merged)

    # 更新 meta.json
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
    print(f"  ✓ 元数据已更新")

    return True


def _safe_float(val):
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


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

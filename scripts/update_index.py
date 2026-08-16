# /// script
# requires-python = ">=3.11"
# dependencies = ["akshare"]
# ///
"""
指数投资工作台 - 数据更新脚本

数据源 (全部通过 AKShare):
  - 量价数据 (收盘价/成交量): stock_zh_index_daily_tx → 腾讯财经
  - 估值数据 (PE/PB/中位数): stock_index_pe_lg / stock_index_pb_lg → Legulegu (理杏仁)

用法:
    uv run scripts/update_index.py                # 增量更新所有启用指数
    uv run scripts/update_index.py --full          # 全量重新获取
    uv run scripts/update_index.py --index h00300  # 只更新指定指数
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "indices.json"

CSV_COLUMNS = [
    "date", "close", "volume", "amount",
    "pe_ttm", "pe_median", "pb", "pb_median",
    "dividend_yield",
]


# ── AKShare 数据获取 ───────────────────────────────────────────
def fetch_price_akshare(tx_symbol):
    """从 AKShare (腾讯财经) 获取指数量价数据。

    返回: [{date, close, volume}] 列表，按日期升序
    """
    import akshare as ak

    df = ak.stock_zh_index_daily_tx(symbol=tx_symbol)
    print(f"    腾讯量价: {len(df)} 条", flush=True)

    rows = []
    for _, row in df.iterrows():
        date_str = str(row["date"])[:10]  # YYYY-MM-DD
        rows.append({
            "date": date_str,
            "close": _safe_float(row.get("close")),
            "volume": _safe_float(row.get("amount")),  # 腾讯接口 amount 实为成交量(手)
        })
    return rows


def fetch_valuation_akshare(lg_symbol):
    """从 AKShare (Legulegu 理杏仁) 获取指数 PE/PB 估值数据。

    返回: {date_str: {pe_ttm, pe_median, pb, pb_median}} 字典
    """
    import akshare as ak

    valuation = {}

    # ── PE 数据 ──
    try:
        pe_df = ak.stock_index_pe_lg(symbol=lg_symbol)
        print(f"    Legulegu PE: {len(pe_df)} 条", flush=True)
        for _, row in pe_df.iterrows():
            date_str = str(row["日期"])[:10]
            valuation[date_str] = {
                "pe_ttm": _safe_float(row.get("滚动市盈率")),
                "pe_median": _safe_float(row.get("滚动市盈率中位数")),
            }
    except Exception as e:
        print(f"  ⚠ AKShare PE 获取失败: {e}")

    # ── PB 数据 ──
    try:
        pb_df = ak.stock_index_pb_lg(symbol=lg_symbol)
        print(f"    Legulegu PB: {len(pb_df)} 条", flush=True)
        for _, row in pb_df.iterrows():
            date_str = str(row["日期"])[:10]
            if date_str not in valuation:
                valuation[date_str] = {}
            valuation[date_str]["pb"] = _safe_float(row.get("市净率"))
            valuation[date_str]["pb_median"] = _safe_float(row.get("市净率中位数"))
    except Exception as e:
        print(f"  ⚠ AKShare PB 获取失败: {e}")

    return valuation


# ── CSV 读写 ───────────────────────────────────────────────────
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
    tx_symbol = cfg.get("tx_symbol", f"sh{cfg.get('price_index_code', '000300')}")
    lg_symbol = cfg.get("akshare_symbol", "")

    today_fmt = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"更新指数: {name} ({code})")
    print(f"  量价源: AKShare/腾讯 ({tx_symbol})")
    print(f"  估值源: AKShare/Legulegu ({lg_symbol or '未配置'})")
    print(f"  数据目录: {data_dir}")

    last_date = get_last_date(csv_path) if not force_full else None

    if last_date:
        print(f"  模式: 增量更新 (上次: {last_date} -> {today_fmt})")
    else:
        print(f"  模式: 全量初始化 ({start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} -> {today_fmt})")

    # ── 1. 获取量价数据 (AKShare/腾讯) ──
    print(f"  [1/2] 获取量价数据...")
    price_rows = fetch_price_akshare(tx_symbol)
    if not price_rows:
        print(f"  ✗ 量价数据获取失败")
        return False
    print(f"  ✓ 量价数据: {len(price_rows)} 条 ({price_rows[0]['date']} ~ {price_rows[-1]['date']})")

    # ── 2. 获取估值数据 (AKShare/Legulegu) ──
    valuation_map = {}
    if lg_symbol:
        print(f"  [2/2] 获取估值数据...")
        valuation_map = fetch_valuation_akshare(lg_symbol)
        print(f"  ✓ 估值数据: {len(valuation_map)} 条")
    else:
        print(f"  [2/2] 跳过估值数据 (未配置 akshare_symbol)")

    # ── 3. 合并量价 + 估值，按 start_date 过滤 ──
    start_str = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}" if len(start_date) == 8 else start_date

    merged = []
    pe_count = 0
    pb_count = 0
    for p in price_rows:
        if p["date"] < start_str:
            continue

        val = valuation_map.get(p["date"], {})
        pe_ttm = val.get("pe_ttm", "")
        pe_median = val.get("pe_median", "")
        pb = val.get("pb", "")
        pb_median = val.get("pb_median", "")

        if pe_ttm:
            pe_count += 1
        if pb:
            pb_count += 1

        merged.append({
            "date": p["date"],
            "close": p["close"] or "",
            "volume": p["volume"] or "",
            "amount": "",
            "pe_ttm": pe_ttm or "",
            "pe_median": pe_median or "",
            "pb": pb or "",
            "pb_median": pb_median or "",
            "dividend_yield": "",
        })

    total = len(merged)
    if total == 0:
        print(f"  ✗ 合并后无数据 (start_date={start_str})")
        return False

    print(f"  估值覆盖: PE={pe_count}/{total} ({pe_count*100//total}%), "
          f"PB={pb_count}/{total} ({pb_count*100//total}%)")

    # ── 4. 写入 CSV ──
    if force_full or not csv_path.exists():
        write_csv(csv_path, merged)
    else:
        append_csv(csv_path, merged)

    # ── 5. 更新 meta.json ──
    meta = {
        "code": code,
        "name": name,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_records": len(read_existing_dates(csv_path)),
        "last_date": get_last_date(csv_path),
        "data_source": "akshare/tencent (price) + akshare/legulegu (valuation)",
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
        result = float(val)
        if result != result:  # NaN check
            return None
        return result
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

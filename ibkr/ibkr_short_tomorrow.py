import requests
import json
import time

requests.packages.urllib3.disable_warnings()
BASE_URL = "https://localhost:5000/v1/api"

# S&P 500 Top 100 by Market Cap
SP500_TOP100 = {
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","TSLA","BRK.B","AVGO",
    "LLY","JPM","V","UNH","XOM","MA","COST","HD","WMT","PG",
    "NFLX","JNJ","ORCL","CRM","BAC","MRK","CVX","ABBV","AMD","KO",
    "ACN","TMO","WFC","MCD","ADBE","PEP","LIN","CSCO","DIS","ABT",
    "GE","QCOM","TXN","PM","IBM","NOW","INTU","DHR","GS","AMAT",
    "AMGN","ISRG","CAT","RTX","MS","SPGI","NEE","T","LOW","UNP",
    "HON","VRTX","BLK","AXP","GILD","ELV","DE","PFE","BSX","BKNG",
    "SYK","MDT","ADI","MMC","TJX","ADP","BMY","C","PLD","SCHW",
    "UBER","MU","CB","SO","CI","CME","INTC","ETN","EOG","ZTS",
    "COP","REGN","FI","ITW","APD","WM","MCO","GD","NOC","PGR"
}

# Fields we want:
# 31  = last price
# 83  = % change today
# 7293 = 52wk high
# 7294 = 52wk low
# 7296 = % off 52wk high
FIELDS = "31,83,7293,7294,7296"

def get_conid(symbol):
    try:
        r = requests.post(f"{BASE_URL}/iserver/secdef/search",
                          json={"symbol": symbol, "secType": "STK"},
                          verify=False)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0].get("conid")
    except:
        pass
    return None

def get_snapshot(conids_str):
    try:
        r = requests.get(
            f"{BASE_URL}/iserver/marketdata/snapshot",
            params={"conids": conids_str, "fields": FIELDS},
            verify=False
        )
        return r.json()
    except:
        return []

# Step 1: Get conids
print("Getting conids for S&P 500 Top 100...")
conid_map = {}
for sym in SP500_TOP100:
    cid = get_conid(sym)
    if cid:
        conid_map[sym] = str(cid)
    time.sleep(0.1)
print(f"Got {len(conid_map)} conids")

# Step 2: First pass — subscribe (IBKR requires 2 requests, first warms up)
symbols_list = list(conid_map.keys())
batch_size = 20
all_conids_batches = []

for i in range(0, len(symbols_list), batch_size):
    batch_syms = symbols_list[i:i+batch_size]
    batch_conids = ",".join(conid_map[s] for s in batch_syms)
    all_conids_batches.append((batch_syms, batch_conids))

print("First pass (subscribing to market data)...")
for batch_syms, batch_conids in all_conids_batches:
    get_snapshot(batch_conids)
    time.sleep(0.3)

# Wait for data to populate
print("Waiting 5 seconds for data to populate...")
time.sleep(5)

# Step 3: Second pass — get actual data
print("Second pass (fetching real data)...")
all_snapshots = {}
for batch_syms, batch_conids in all_conids_batches:
    snaps = get_snapshot(batch_conids)
    if isinstance(snaps, list):
        for snap in snaps:
            cid = str(snap.get("conid", ""))
            for sym, c in conid_map.items():
                if c == cid:
                    all_snapshots[sym] = snap
                    break
    time.sleep(0.3)

print(f"Got data for {len(all_snapshots)} stocks")

# Step 4: Print raw sample to check field names
print("\nSample raw data (first 3 stocks):")
for sym, snap in list(all_snapshots.items())[:3]:
    print(f"  {sym}: {snap}")

# Step 5: Score candidates
def clean_float(val):
    try:
        return float(str(val).replace("%","").replace("−","-").replace("–","-").replace("+","").strip())
    except:
        return None

candidates = []
for sym, snap in all_snapshots.items():
    last_price   = clean_float(snap.get("31"))
    high_52wk    = clean_float(snap.get("7293"))
    today_chg    = clean_float(snap.get("83"))

    if last_price is None or high_52wk is None or today_chg is None:
        continue
    if high_52wk == 0:
        continue

    # Calculate % off 52wk high manually
    pct_off_high = ((last_price - high_52wk) / high_52wk) * 100

    # In downtrend (>10% off 52wk high) but NOT sold today (flat or up)
    if pct_off_high <= -10 and today_chg > -1.0:
        candidates.append({
            "symbol":            sym,
            "pct_off_52wk_high": pct_off_high,
            "today_chg":         today_chg,
            "last_price":        last_price,
            "high_52wk":         high_52wk,
            "is_up_today":       today_chg > 0.5
        })

candidates.sort(key=lambda x: x["pct_off_52wk_high"])

print("\n========================================")
print("  🎯 SHORT CANDIDATES FOR TOMORROW")
print("  In downtrend (>10% off 52wk high)")
print("  BUT flat/up today = NOT sold yet")
print("========================================\n")

if candidates:
    print(f"{'Symbol':<8} {'% Off 52wk High':<18} {'Today %Chg':<14} {'Price':<10} {'52wk High':<12} Signal")
    print("-" * 75)
    for c in candidates:
        signal = "🔥 UP today" if c["is_up_today"] else "→ Flat today"
        print(f"{c['symbol']:<8} {c['pct_off_52wk_high']:<18.1f} {c['today_chg']:<14.2f} ${c['last_price']:<9} ${c['high_52wk']:<11} {signal}")

    print(f"\n🏆 TOP PICK: {candidates[0]['symbol']} — {candidates[0]['pct_off_52wk_high']:.1f}% off 52wk high, {candidates[0]['today_chg']:+.2f}% today")
else:
    print("No candidates found")

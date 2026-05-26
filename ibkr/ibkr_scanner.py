import requests
import json
import time

requests.packages.urllib3.disable_warnings()
BASE_URL = "https://localhost:5000/v1/api"

# S&P 500 Top 100 by Market Cap (hardcoded)
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

def get_symbols(result):
    symbols = set()
    try:
        contracts = result.get("contracts", [])
        for item in contracts:
            sym = item.get("symbol", "").strip()
            if sym:
                symbols.add(sym)
    except Exception as e:
        print(f"  Parse error: {e}")
    return symbols

# Scan top losers - large cap only (market cap > $10B)
print("--- Scanning Top Losers (Large Cap) ---")
losers = requests.post(f"{BASE_URL}/iserver/scanner/run", verify=False, json={
    "instrument": "STK",
    "type": "TOP_PERC_LOSE",
    "filter": [
        {"code": "priceAbove", "value": 10},
        {"code": "avgVolumeAbove", "value": 500000},
        {"code": "marketCapAbove1e6", "value": 10000}
    ],
    "location": "STK.US.MAJOR",
    "size": "100"
}).json()
time.sleep(1)

# Scan top % gainers (reversal/short fade candidates)
print("--- Scanning Top Gainers (reversal candidates) ---")
gainers = requests.post(f"{BASE_URL}/iserver/scanner/run", verify=False, json={
    "instrument": "STK",
    "type": "TOP_PERC_GAIN",
    "filter": [
        {"code": "priceAbove", "value": 10},
        {"code": "avgVolumeAbove", "value": 500000},
        {"code": "marketCapAbove1e6", "value": 10000}
    ],
    "location": "STK.US.MAJOR",
    "size": "100"
}).json()
time.sleep(1)

# Scan most active (high volume = high interest)
print("--- Scanning Most Active ---")
active = requests.post(f"{BASE_URL}/iserver/scanner/run", verify=False, json={
    "instrument": "STK",
    "type": "MOST_ACTIVE",
    "filter": [
        {"code": "priceAbove", "value": 10},
        {"code": "marketCapAbove1e6", "value": 10000}
    ],
    "location": "STK.US.MAJOR",
    "size": "100"
}).json()
time.sleep(1)

# Extract symbols
loser_syms  = get_symbols(losers)
gainer_syms = get_symbols(gainers)
active_syms = get_symbols(active)

print(f"\nLosers found:  {len(loser_syms)}  → {loser_syms}")
print(f"Gainers found: {len(gainer_syms)} → {gainer_syms}")
print(f"Active found:  {len(active_syms)} → {active_syms}")

# Cross-reference with S&P 500 Top 100
losers_in_sp500  = loser_syms  & SP500_TOP100
gainers_in_sp500 = gainer_syms & SP500_TOP100
active_in_sp500  = active_syms & SP500_TOP100

print("\n========================================")
print("   SHORT CANDIDATES — S&P 500 TOP 100")
print("========================================")

print("\n📉 TOP LOSERS in S&P 500 Top 100 (momentum short):")
if losers_in_sp500:
    for s in sorted(losers_in_sp500):
        print(f"  → {s}")
else:
    print("  None found")

print("\n🔄 TOP GAINERS in S&P 500 Top 100 (reversal/fade short):")
if gainers_in_sp500:
    for s in sorted(gainers_in_sp500):
        print(f"  → {s}")
else:
    print("  None found")

print("\n🎯 BEST SHORT CANDIDATES (losing + high volume):")
best = losers_in_sp500 & active_in_sp500
if best:
    for s in sorted(best):
        print(f"  ★ {s}")
else:
    # fallback: just show losers
    print("  Showing top losers in S&P 500 Top 100:")
    for s in sorted(losers_in_sp500):
        print(f"  ★ {s}")

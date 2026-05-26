import pandas as pd
import yfinance as yf
import chardet
import datetime
import numpy as np
import csv
import re
import pytz

class DataEngine:
    def __init__(self):
        self.df = None
        self.mapped_data = []
        self.column_mapping = {}
        
        # Internal standard keys
        # Added 'Current Price' and 'Market Cap' for the Thin Client mode
        self.required_fields = [
            "Signal Timestamp", 
            "Signal Timeframe", 
            "Symbol", 
            "Direction", 
            "Signal Price", 
            "Optimum Price", 
            "Stop Loss Price", 
            "Take Profit Price", 
            "Break Possibility",
            "Current Price",
            "Market Cap"
        ]

    # --- HELPER UTILS ---
    @staticmethod
    def parse_human_number(s):
        """
        Parses strings like '100B', '1.5T', '500M' into floats.
        Returns None if parsing fails.
        """
        if isinstance(s, (int, float)):
            return float(s)
        
        if not isinstance(s, str) or not s.strip():
            return None
            
        s = s.upper().strip().replace(',', '')
        multipliers = {'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12}
        
        # Check for suffix
        for suffix, mult in multipliers.items():
            if s.endswith(suffix):
                try:
                    num_part = float(s[:-1])
                    return num_part * mult
                except:
                    return None
        
        # Try plain number
        try:
            return float(s)
        except:
            return None

    # --- CSV UTILS ---
    def detect_encoding(self, file_path):
        try:
            if hasattr(file_path, 'read'):
                file_path.seek(0)
                raw = file_path.read(50000)
                file_path.seek(0)
                result = chardet.detect(raw)
                return result['encoding'] or 'utf-8'
            else:
                with open(file_path, 'rb') as rawdata:
                    raw = rawdata.read(50000)
                    result = chardet.detect(raw)
                    return result['encoding'] or 'utf-8'
        except:
            return 'utf-8'

    def detect_separator(self, file_path, encoding):
        candidates = [',', ';', '\t', '|']
        best_sep = ','
        max_cols = 0
        try:
            if hasattr(file_path, 'read'):
                file_path.seek(0)
                line = file_path.readline().decode(encoding, errors='replace')
                file_path.seek(0)
            else:
                with open(file_path, 'r', encoding=encoding, errors='replace') as f:
                    line = f.readline()
            
            if not line: return ','
            
            for sep in candidates:
                try:
                    reader = csv.reader([line], delimiter=sep)
                    cols = next(reader)
                    if len(cols) > max_cols:
                        max_cols = len(cols)
                        best_sep = sep
                except:
                    continue
        except:
            pass
        return best_sep

    def load_csv_headers(self, file_path, manual_sep=None):
        encoding = self.detect_encoding(file_path)
        sep = manual_sep if manual_sep else self.detect_separator(file_path, encoding)
            
        try:
            if hasattr(file_path, 'seek'):
                file_path.seek(0)
                
            df = pd.read_csv(file_path, nrows=0, encoding=encoding, sep=sep)
            return list(df.columns), encoding, sep
        except:
            try:
                if hasattr(file_path, 'seek'):
                    file_path.seek(0)
                df = pd.read_csv(file_path, nrows=0, encoding=encoding, sep=None, engine='python')
                return list(df.columns), encoding, None
            except:
                return [], None, None

    def clean_symbol(self, symbol):
        if not isinstance(symbol, str): return str(symbol)
        clean = symbol.upper()
        clean = clean.replace(".US", "").replace(".UK", "").replace("NAS100", "NQ=F")
        return clean

    def auto_map_columns(self, headers):
        mapping = {}
        headers_lower = [str(h).lower().strip() for h in headers]
        
        # Updated patterns to prioritize the specific names requested
        field_patterns = {
            "Signal Timestamp": ["date", "signal timestamp", "timestamp", "time"],
            "Signal Timeframe": ["timeframe", "signal timeframe", "tf", "interval"],
            "Symbol": ["symbol", "ticker", "stock"],
            "Direction": ["direction", "dir", "long/short"],
            "Signal Price": ["signal price", "entry", "entry price", "price"],
            "Optimum Price": ["optimum price", "optimum", "target"],
            "Stop Loss Price": ["stop loss price", "stop loss", "sl", "sl price"],
            "Take Profit Price": ["tp price", "tp", "take profit", "take profit price"],
            "Break Possibility": ["breakout possibility", "break possibility", "break", "breakout"],
            "Current Price": ["current price", "curr", "last price", "current"],
            "Market Cap": ["market cap", "mcap", "cap"]
        }
        
        used_indices = set()
        
        for field in self.required_fields:
            patterns = field_patterns.get(field, [field.lower()])
            found = False
            best_match_idx = None
            
            for pattern in patterns:
                for idx, h_lower in enumerate(headers_lower):
                    if idx not in used_indices:
                        # Exact match or contained match logic
                        if pattern == h_lower or pattern in h_lower:
                            best_match_idx = idx
                            found = True
                            break
                if found:
                    break
            
            if not found:
                # Fallback fuzzy match
                norm_field = field.lower().replace(" ", "").replace("_", "")
                for idx, h in enumerate(headers):
                    if idx not in used_indices:
                        norm_h = str(h).lower().replace(" ", "").replace("_", "")
                        if norm_field in norm_h:
                            best_match_idx = idx
                            found = True
                            break
            
            if found and best_match_idx is not None:
                mapping[field] = headers[best_match_idx]
                used_indices.add(best_match_idx)
            else:
                pass 
        
        return mapping

    def parse_custom_date(self, date_val, is_gsheet=False):
        if isinstance(date_val, (pd.Timestamp, datetime.datetime)):
             naive_dt = date_val
        else:
            if pd.isna(date_val) or str(date_val).strip() == "": 
                return None
            
            original_str = str(date_val).strip()
            
            # 1. Try ISO/UTC format string directly (e.g., 2025-05-19 00:00:00+00:00)
            try:
                dt = pd.to_datetime(original_str)
                if dt is not pd.NaT:
                    if dt.tzinfo is not None:
                        return dt.tz_convert(pytz.utc)
                    else:
                        naive_dt = dt
            except:
                naive_dt = None

            # 2. Fallback to custom parsing
            if naive_dt is None:
                clean_str = re.sub(r'\s+', ' ', original_str)
                try:
                    date_part = clean_str
                    time_part = "00:00:00"
                    if ' ' in clean_str:
                        date_part, time_part = clean_str.split(' ', 1)
                    
                    date_part = date_part.replace('/', '.').replace('-', '.')
                    parts = date_part.split('.')
                    
                    if len(parts) == 3:
                        p1, p2, p3 = parts[0], parts[1], parts[2]
                        if len(p1) == 4:
                            year, month, day = int(p1), int(p2), int(p3)
                        else:
                            day, month, year = int(p1), int(p2), int(p3)
                        
                        hour, minute, second = 0, 0, 0
                        if ':' in time_part:
                            t_parts = time_part.split(':')
                            if len(t_parts) >= 2:
                                hour = int(t_parts[0])
                                minute = int(t_parts[1])
                            if len(t_parts) == 3:
                                second = int(float(t_parts[2].split('+')[0]))
                        naive_dt = datetime.datetime(year, month, day, hour, minute, second)
                except:
                    pass

        if naive_dt is None or naive_dt is pd.NaT:
            return None

        try:
            if naive_dt.tzinfo is None:
                if is_gsheet:
                    est_tz = pytz.timezone('US/Eastern')
                    local_dt = est_tz.localize(naive_dt)
                else:
                    gmt_plus_2 = pytz.FixedOffset(120) 
                    local_dt = gmt_plus_2.localize(naive_dt)
            else:
                local_dt = naive_dt

            utc_dt = local_dt.astimezone(pytz.utc)
            return utc_dt
        except:
            return None

    def process_dataframe(self, df, mapping, is_gsheet=False):
        self.df = df
        self.mapped_data = []
        
        for index, row in self.df.iterrows():
            signal = {}
            for internal, csv_col in mapping.items():
                val = row.get(csv_col, None)
                if internal == "Symbol":
                    val = self.clean_symbol(val)
                
                # Ensure numeric cleaning for all price/probability fields
                if "Price" in internal or "Possibility" in internal:
                    try:
                        clean_val = str(val).replace(',', '').replace('%','').strip()
                        val = float(clean_val)
                    except:
                        val = 0.0
                
                # Specific cleaning for Market Cap
                if internal == "Market Cap":
                    val = self.parse_human_number(val)
                
                signal[internal] = val

            raw_ts = signal.get('Signal Timestamp')
            parsed_ts = self.parse_custom_date(raw_ts, is_gsheet=is_gsheet) 
            
            if parsed_ts is None: continue
            
            signal['Signal Timestamp'] = parsed_ts
            signal['Original Index'] = index
            self.mapped_data.append(signal)
        
        if not self.mapped_data:
            return False, "No valid signals found."
        return True, f"Processed {len(self.mapped_data)} valid signals."

    def load_csv_and_process(self, file_path, mapping, encoding, sep):
        engine_type = 'python' if sep is None else 'c'
        try:
            if hasattr(file_path, 'seek'):
                file_path.seek(0)
            
            df_raw = pd.read_csv(file_path, encoding=encoding, sep=sep, engine=engine_type, dtype=str)
            return self.process_dataframe(df_raw, mapping, is_gsheet=False)
        except Exception as e:
            return False, f"Error: {str(e)}"

    def get_signals(self):
        return self.mapped_data

    # --- HISTORICAL DATA UTILS ---

    def _convert_tf_to_interval(self, tf, override=None):
        if override and override != "Default": return override
        
        tf_str = str(tf).upper().replace(" ", "").replace("-", "")
        if '1HOUR' in tf_str or '1H' in tf_str or 'H1' in tf_str: return '1h'
        if '4HOUR' in tf_str or '4H' in tf_str or 'H4' in tf_str: return '1h' 
        if 'DAILY' in tf_str or 'D1' in tf_str: return '1d'
        if '30M' in tf_str: return '30m'
        if '15M' in tf_str: return '15m'
        if '5M' in tf_str: return '5m'
        if '1W' in tf_str: return '1wk'
        return '1d'

    def fetch_candle_data(self, symbol, timeframe, start_date, lookback_bars=50, override_interval=None):
        interval = self._convert_tf_to_interval(timeframe, override_interval)
        
        delta = datetime.timedelta(days=1)
        if interval == '1h': delta = datetime.timedelta(hours=1)
        elif interval == '30m': delta = datetime.timedelta(minutes=30)
        elif interval == '15m': delta = datetime.timedelta(minutes=15)
        elif interval == '5m': delta = datetime.timedelta(minutes=5)
        elif interval == '1wk': delta = datetime.timedelta(weeks=1)
        
        now_utc = datetime.datetime.now(pytz.utc)
        start_fetch = start_date - (delta * lookback_bars * 3)
        end_fetch_cap = now_utc + datetime.timedelta(days=1) 
        
        if start_fetch > now_utc:
            return None, "Signal/Lookback is in the future"

        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(interval=interval, start=start_fetch, end=end_fetch_cap)
            if hist.empty: return None, f"No data for {symbol}"
            
            hist.reset_index(inplace=True)
            col_map = {c.lower(): c for c in hist.columns}
            date_col = col_map.get('date') or col_map.get('datetime')
            if not date_col: return None, "No Date column"
            
            hist[date_col] = pd.to_datetime(hist[date_col]).dt.tz_convert(pytz.utc)
            hist.set_index(date_col, inplace=True)
            
            requested_interval = '4h' if ('4H' in str(timeframe).upper() and override_interval is None) or override_interval == '4h' else interval
            if requested_interval == '4h' and interval == '1h':
                agg_dict = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
                hist = hist.resample('4h', closed='left', label='left').agg(agg_dict).dropna()

            return hist, None
        except Exception as e:
            return None, str(e)

    def calculate_market_expiry(self, start_utc, bars, timeframe_str):
        tf_str = str(timeframe_str).upper().replace(" ", "").replace("-", "")
        
        if 'DAILY' in tf_str or 'D1' in tf_str or 'W' in tf_str:
            offset = pd.tseries.offsets.BusinessDay(bars)
            expiry_utc = start_utc + offset
            return expiry_utc

        minutes_per_bar = 60 
        if '4H' in tf_str: minutes_per_bar = 240
        elif '30M' in tf_str: minutes_per_bar = 30
        elif '15M' in tf_str: minutes_per_bar = 15
        elif '5M' in tf_str: minutes_per_bar = 5
        
        est = pytz.timezone('US/Eastern')
        current_dt = start_utc.astimezone(est)
        
        market_open_time = datetime.time(9, 30)
        market_close_time = datetime.time(16, 0)
        
        bars_remaining = bars
        
        while bars_remaining > 0:
            current_dt += datetime.timedelta(minutes=minutes_per_bar)
            
            is_weekend = current_dt.weekday() >= 5 
            is_past_close = current_dt.time() > market_close_time
            is_before_open = current_dt.time() < market_open_time
            
            if is_weekend or is_past_close:
                current_dt += datetime.timedelta(days=1)
                current_dt = current_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                while current_dt.weekday() >= 5:
                    current_dt += datetime.timedelta(days=1)
            
            elif is_before_open:
                 current_dt = current_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                 current_dt += datetime.timedelta(minutes=minutes_per_bar)

            bars_remaining -= 1
            
        return current_dt.astimezone(pytz.utc)

    def determine_status(self, signal, expiry_bars=20):
        tf = signal['Signal Timeframe']
        ts = signal['Signal Timestamp']
        expiry_time = self.calculate_market_expiry(ts, expiry_bars, tf)
        
        now = datetime.datetime.now(pytz.utc)
        
        if now > expiry_time:
            return "Expired", expiry_time
        else:
            return "Active", expiry_time

    # --- SIMULATION UTILS ---

    def get_price_data_batch(self, symbol, start_utc, end_utc, interval='1d'):
        try:
            fetch_start = start_utc - datetime.timedelta(days=5)
            fetch_end = end_utc + datetime.timedelta(days=5)
            
            ticker = yf.Ticker(symbol)
            hist = ticker.history(interval=interval, start=fetch_start, end=fetch_end)
            
            if hist.empty: return pd.DataFrame()
            
            hist.reset_index(inplace=True)
            col_map = {c.lower(): c for c in hist.columns}
            date_col = col_map.get('date') or col_map.get('datetime')
            
            if not date_col: return pd.DataFrame()
            
            hist[date_col] = pd.to_datetime(hist[date_col]).dt.tz_convert(pytz.utc)
            hist.set_index(date_col, inplace=True)
            return hist
        except:
            return pd.DataFrame()

    def run_simulation_on_signal(self, signal, expiry_bars, sim_bars, tf_override=None, price_mode='close', monte_carlo_opts=None, mc_iterations=1):
        symbol = signal['Symbol']
        sig_ts = signal['Signal Timestamp']
        direction = str(signal.get('Direction', '')).upper()
        sig_price = signal.get('Signal Price', 0)
        opt_price = signal.get('Optimum Price', 0)
        sl_price = signal.get('Stop Loss Price', 0)
        tf_native = signal['Signal Timeframe']
        
        if sig_price == 0: return None

        expiry_ts = self.calculate_market_expiry(sig_ts, expiry_bars, tf_native)
        check_interval = self._convert_tf_to_interval(tf_native)
        df_check = self.get_price_data_batch(symbol, sig_ts, expiry_ts, interval=check_interval)
        sim_interval = self._convert_tf_to_interval(tf_override) if tf_override else check_interval
        sim_end_est = expiry_ts + datetime.timedelta(days=sim_bars * 2 if sim_interval == '1d' else 5)
        df_sim_original = self.get_price_data_batch(symbol, sig_ts, sim_end_est, interval=sim_interval)
        
        if df_sim_original.empty: return None

        def run_calc(entry_price, start_time, df_target):
            if entry_price == 0: return None
            
            future_data = df_target[df_target.index >= start_time].copy()
            if future_data.empty: return None
            future_data = future_data.iloc[:sim_bars] 
            
            pnl_percent = []
            dates = []
            mae_val = 0.0
            mfe_val = 0.0
            
            for dt, row in future_data.iterrows():
                current_p = row['Close']
                if price_mode == 'open': current_p = row['Open']
                elif price_mode == 'high': current_p = row['High']
                elif price_mode == 'low': current_p = row['Low']
                elif price_mode == 'optimistic':
                    current_p = row['High'] if 'LONG' in direction else row['Low']
                elif price_mode == 'pessimistic':
                    current_p = row['Low'] if 'LONG' in direction else row['High']
                
                if 'LONG' in direction: val = ((current_p - entry_price) / entry_price) * 100
                elif 'SHORT' in direction: val = ((entry_price - current_p) / entry_price) * 100
                else: val = 0.0
                
                pnl_percent.append(val)
                dates.append(dt)
                
                if 'LONG' in direction:
                    curr_mae = ((row['Low'] - entry_price) / entry_price) * 100
                    curr_mfe = ((row['High'] - entry_price) / entry_price) * 100
                    if curr_mae < mae_val: mae_val = curr_mae
                    if curr_mfe > mfe_val: mfe_val = curr_mfe
                elif 'SHORT' in direction:
                    curr_mae = ((entry_price - row['High']) / entry_price) * 100
                    curr_mfe = ((entry_price - row['Low']) / entry_price) * 100
                    if curr_mae < mae_val: mae_val = curr_mae
                    if curr_mfe > mfe_val: mfe_val = curr_mfe

            return { "pnl": pnl_percent, "dates": dates, "mae": mae_val, "mfe": mfe_val }

        hit_times = {"Signal": sig_ts, "Optimum": None, "SL": None}

        if opt_price > 0 and not df_check.empty:
            mask_window = (df_check.index >= sig_ts) & (df_check.index <= expiry_ts)
            window_data = df_check[mask_window]
            for idx, row in window_data.iterrows():
                if 'LONG' in direction:
                    if row['Low'] <= opt_price <= row['High']: hit_times["Optimum"] = idx; break
                elif 'SHORT' in direction:
                    if row['Low'] <= opt_price <= row['High']: hit_times["Optimum"] = idx; break
                    
        if sl_price > 0 and not df_check.empty:
            mask_window = (df_check.index >= sig_ts) & (df_check.index <= expiry_ts)
            window_data = df_check[mask_window]
            for idx, row in window_data.iterrows():
                if row['Low'] <= sl_price <= row['High']: hit_times["SL"] = idx; break

        final_results = {"Signal": None, "Optimum": None, "SL": None}
        iters = mc_iterations if (monte_carlo_opts and monte_carlo_opts.get('enabled', False)) else 1
        
        accumulators = {
            "Signal": {"pnl_sum": None, "mae_sum": 0.0, "mfe_sum": 0.0, "count": 0, "dates": []},
            "Optimum": {"pnl_sum": None, "mae_sum": 0.0, "mfe_sum": 0.0, "count": 0, "dates": []},
            "SL": {"pnl_sum": None, "mae_sum": 0.0, "mfe_sum": 0.0, "count": 0, "dates": []}
        }

        for _ in range(iters):
            df_curr = df_sim_original.copy()
            if monte_carlo_opts and monte_carlo_opts.get('enabled', False):
                if monte_carlo_opts.get('reshuffle', False):
                    returns = df_curr['Close'].pct_change().fillna(0).to_numpy()
                    spread_open = (df_curr['Open'] / df_curr['Close']).to_numpy()
                    spread_high = (df_curr['High'] / df_curr['Close']).to_numpy()
                    spread_low  = (df_curr['Low'] / df_curr['Close']).to_numpy()

                    permuted_indices = np.random.permutation(len(returns))
                    shuffled_returns = returns[permuted_indices]
                    shuffled_s_open = spread_open[permuted_indices]
                    shuffled_s_high = spread_high[permuted_indices]
                    shuffled_s_low = spread_low[permuted_indices]

                    start_price = df_curr['Close'].iloc[0]
                    cum_returns = np.cumprod(1 + shuffled_returns)
                    new_close = start_price * cum_returns

                    df_curr['Close'] = new_close
                    df_curr['Open'] = new_close * shuffled_s_open
                    df_curr['High'] = new_close * shuffled_s_high
                    df_curr['Low'] = new_close * shuffled_s_low
                
                noise_pct = monte_carlo_opts.get('noise_pct', 0.0)
                if noise_pct > 0:
                    scale = noise_pct / 100.0
                    for c in ['Open', 'High', 'Low', 'Close']:
                        noise = np.random.normal(0, scale, size=len(df_curr))
                        df_curr[c] = df_curr[c] * (1 + noise)

            prices = {"Signal": sig_price, "Optimum": opt_price, "SL": sl_price}
            
            for key in ["Signal", "Optimum", "SL"]:
                start_t = hit_times[key]
                if start_t is not None:
                    res = run_calc(prices[key], start_t, df_curr)
                    if res:
                        acc = accumulators[key]
                        if acc["pnl_sum"] is None:
                            acc["pnl_sum"] = np.zeros(len(res["pnl"]))
                            acc["dates"] = res["dates"] 
                        
                        curr_pnl = np.array(res["pnl"])
                        if len(curr_pnl) == len(acc["pnl_sum"]):
                            acc["pnl_sum"] += curr_pnl
                            acc["mae_sum"] += res["mae"]
                            acc["mfe_sum"] += res["mfe"]
                            acc["count"] += 1
        
        for key in ["Signal", "Optimum", "SL"]:
            acc = accumulators[key]
            if acc["count"] > 0:
                final_results[key] = {
                    "pnl": (acc["pnl_sum"] / acc["count"]).tolist(),
                    "dates": acc["dates"],
                    "mae": acc["mae_sum"] / acc["count"],
                    "mfe": acc["mfe_sum"] / acc["count"]
                }
        
        return final_results

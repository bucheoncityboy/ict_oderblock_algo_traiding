import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
import ccxt
import pandas as pd
import time
import threading
from queue import Queue
from playsound import playsound
import os

# -----------------------------------------------------------------------------
# 트레이딩 봇 로직 클래스
# -----------------------------------------------------------------------------
class TradingBot:
    def __init__(self, api_key, api_secret, params, msg_queue):
        self.api_key = api_key
        self.api_secret = api_secret
        self.msg_queue = msg_queue
        
        self.symbol = params['symbol']
        self.timeframe = params['timeframe']
        self.trend_timeframe = params['trend_timeframe']
        self.rr_ratio = params['rr_ratio']
        self.risk_per_trade_usd = params['risk_per_trade_usd']
        self.reinvestment_percent = params['reinvestment_percent']
        self.initial_capital = params['initial_capital']
        self.ob_entry_level = params['ob_entry_level']
        
        self.reinvestment_target_achieved = False
        self.consecutive_reinvestment_wins = 0
        self.last_trade_profit = 0.0
        self.is_reinvestment_trade = False
        self.balance_at_trade_start = 0.0

        self.is_running = False
        self.active_setup = None

        self.exchange = ccxt.gateio({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap', 'settle': 'usdt'},
        })
        self.log(f"게이트아이오 실거래 모드로 연결합니다. 심볼: {self.symbol}")
        self.log(f"초기 자본금: ${self.initial_capital:.2f}")

    def log(self, message):
        self.msg_queue.put(f"LOG: {message}")

    def play_alarm(self):
        self.msg_queue.put("ALARM")

    def get_balance(self):
        try:
            balance = self.exchange.fetch_balance(params={'settle': 'usdt'})
            return balance['total'].get('USDT', 0)
        except Exception as e:
            self.log(f"잔액 조회 오류: {e}")
            return 0
            
    def update_balance_display(self):
        usdt_balance = self.get_balance()
        self.msg_queue.put(f"BALANCE: {usdt_balance:.2f} USDT")

    def fetch_ohlcv(self, timeframe, limit=100):
        try:
            ohlcv = self.exchange.fetch_ohlcv(self.symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            self.log(f"가격 데이터 조회 오류 ({timeframe}): {e}")
            return pd.DataFrame()

    def calculate_position_size(self, entry_price, sl_price):
        current_balance = self.get_balance()
        
        if not self.reinvestment_target_achieved and current_balance >= self.initial_capital * 2:
            self.reinvestment_target_achieved = True
            self.log(f"🎉 재투자 목표 달성! 현재 잔액: ${current_balance:.2f}")
            self.play_alarm()

        risk_amount_usd = self.risk_per_trade_usd
        self.is_reinvestment_trade = False
        
        if self.reinvestment_target_achieved and self.last_trade_profit > 0 and self.consecutive_reinvestment_wins < 2:
            risk_amount_usd = self.last_trade_profit * self.reinvestment_percent
            self.is_reinvestment_trade = True
            self.log(f"🚀 재투자 실행! 직전 수익(${self.last_trade_profit:.2f})의 {self.reinvestment_percent*100}%인 ${risk_amount_usd:.2f}를 리스크로 설정.")
        else:
            self.log(f"🛡️ 고정 리스크 실행. 리스크: ${risk_amount_usd:.2f}")

        price_risk_per_unit = abs(entry_price - sl_price)
        if price_risk_per_unit == 0:
            self.log("오류: 진입가와 손절가가 같아 포지션 크기를 계산할 수 없습니다.")
            return None
        
        position_size_base = risk_amount_usd / price_risk_per_unit
        contract_amount = position_size_base * entry_price
        
        self.log(f"계산된 계약 수량: {contract_amount:.2f}")
        return contract_amount

    def check_for_entry(self):
        df = self.fetch_ohlcv(self.timeframe, limit=31)
        if df.empty: return None

        last_30_candles = df.iloc[-31:-1]
        current_candle = df.iloc[-1]
        high_water_mark = last_30_candles['high'].max()
        
        if current_candle['high'] > high_water_mark:
            self.log(f"돌파 신호 포착! 기준 가격: ${high_water_mark}")
            entry_price = self.exchange.fetch_ticker(self.symbol)['last']
            sl_price = high_water_mark * 0.995

            risk_per_unit = abs(entry_price - sl_price)
            tp_price = entry_price + (risk_per_unit * self.rr_ratio)

            amount = self.calculate_position_size(entry_price, sl_price)
            if not amount or amount <= 0:
                self.log("계산된 주문 수량이 0보다 작아 진입하지 않습니다.")
                return None

            return {
                'side': 'buy', 'entry_price': entry_price, 'sl_price': sl_price,
                'tp_price': tp_price, 'amount': amount
            }
        return None

    def get_position_info(self):
        try:
            positions = self.exchange.fetch_positions(symbols=[self.symbol])
            open_positions = [p for p in positions if float(p['contracts']) != 0]
            if open_positions: return open_positions[0]
        except Exception as e:
            self.log(f"포지션 정보 조회 오류: {e}")
        return None

    def place_entry_order(self, setup):
        try:
            self.log(f"포지션 진입 시도: {setup['side']} {setup['amount']:.2f} contracts of {self.symbol}")
            order = self.exchange.create_market_order(self.symbol, setup['side'], setup['amount'])
            self.log(f"포지션 진입 성공! 진입 가격: approx ${setup['entry_price']:.4f}")
            self.play_alarm()
            return order
        except Exception as e:
            self.log(f"진입 주문 오류: {e}")
            return None

    def place_sl_tp_orders(self, setup):
        try:
            position = self.get_position_info()
            if not position:
                self.log("SL/TP 설정 실패: 포지션 정보를 찾을 수 없음")
                return
            amount = abs(float(position['contracts']))
            side = 'sell' if float(position['contracts']) > 0 else 'buy'
            sl_params = {'reduce_only': True, 'stopPrice': setup['sl_price']}
            tp_params = {'reduce_only': True, 'stopPrice': setup['tp_price']}
            self.log(f"손절 주문 설정: ${setup['sl_price']:.4f}")
            self.exchange.create_order(self.symbol, 'stop_market', side, amount, params=sl_params)
            self.log(f"익절 주문 설정: ${setup['tp_price']:.4f}")
            self.exchange.create_order(self.symbol, 'take_profit_market', side, amount, params=tp_params)
            self.log("SL/TP 주문 설정 완료.")
        except Exception as e:
            self.log(f"SL/TP 주문 설정 오류: {e}")

    def close_position_market(self):
        """현재 포지션을 시장가로 즉시 종료합니다."""
        position = self.get_position_info()
        if not position:
            self.log("종료할 포지션이 없습니다.")
            return

        side = 'sell' if float(position['contracts']) > 0 else 'buy'
        amount = abs(float(position['contracts']))
        
        try:
            self.log(f"시장가 포지션 종료 시도: {side} {amount} contracts")
            # 안전을 위해 모든 대기 주문 취소
            self.exchange.cancel_all_orders(self.symbol)
            self.log("모든 대기 주문을 취소했습니다.")
            # 포지션 종료 주문
            self.exchange.create_market_order(self.symbol, side, amount, {'reduce_only': True})
            self.log("✅ 포지션이 성공적으로 종료되었습니다.")
            self.play_alarm()
            # 포지션 종료 후 상태 초기화
            self.active_setup = None
            self.last_trade_profit = 0
            self.consecutive_reinvestment_wins = 0
        except Exception as e:
            self.log(f"❌ 포지션 종료 중 오류 발생: {e}")

    def run(self):
        self.is_running = True
        self.update_balance_display()

        while self.is_running:
            try:
                position = self.get_position_info()

                if not position:
                    if self.active_setup:
                        self.log("포지션이 청산되었습니다. 손익을 계산합니다...")
                        self.play_alarm()
                        
                        current_balance = self.get_balance()
                        self.last_trade_profit = current_balance - self.balance_at_trade_start
                        
                        if self.last_trade_profit > 0:
                            self.log(f"✅ 거래 이익: ${self.last_trade_profit:.2f}")
                            if self.is_reinvestment_trade:
                                self.consecutive_reinvestment_wins += 1
                                self.log(f"재투자 연속 성공: {self.consecutive_reinvestment_wins}회")
                            else:
                                self.consecutive_reinvestment_wins = 0
                        else:
                            self.log(f"❌ 거래 손실: ${self.last_trade_profit:.2f}")
                            self.consecutive_reinvestment_wins = 0
                            self.last_trade_profit = 0
                        
                        if self.consecutive_reinvestment_wins >= 2:
                            self.log("🔒 2회 연속 재투자 성공! 다음 거래는 고정 리스크로 전환합니다.")

                        self.active_setup = None
                        self.update_balance_display()

                    self.log(f"{self.timeframe}봉 기준, 새로운 진입 신호 탐색 중...")
                    new_setup = self.check_for_entry()
                    if new_setup:
                        self.balance_at_trade_start = self.get_balance()
                        self.active_setup = new_setup
                        entry_order = self.place_entry_order(self.active_setup)
                        if entry_order:
                            time.sleep(3)
                            self.place_sl_tp_orders(self.active_setup)
                            self.update_balance_display()
                else:
                    if not self.active_setup:
                        pos_side = 'buy' if float(position['contracts']) > 0 else 'sell'
                        self.log(f"기존 포지션 발견. 수량: {position['contracts']}, 방향: {pos_side}")
                        self.active_setup = {'side': pos_side}
                    
                    self.log(f"포지션 유지 중... 진입가: ${float(position['entryPrice']):.4f}")
                    self.update_balance_display()

                for _ in range(30):
                    if not self.is_running: break
                    time.sleep(1)

            except Exception as e:
                self.log(f"런타임 오류 발생: {e}")
                time.sleep(10)

        self.log("봇이 정지되었습니다.")

    def stop(self):
        self.is_running = False
        self.log("봇 정지 신호를 받았습니다. 루프를 종료합니다.")

# -----------------------------------------------------------------------------
# GUI 애플리케이션 클래스
# -----------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Auto Trading Bot (Gate.io - Live)")
        self.root.geometry("800x750")

        self.bot_thread = None
        self.msg_queue = Queue()
        self.is_dark_mode = False

        self.FONT_MAIN = ("Helvetica", 11)
        self.FONT_LOG = ("Courier New", 10)

        # --- [오류 수정] 모든 테마 키를 포함하도록 수정 ---
        self.light_theme = {
            "bg": "#f0f0f0", "fg": "#000000", "frame_bg": "#fafafa",
            "entry_bg": "#ffffff", "entry_fg": "#000000",
            "button_bg": "#e0e0e0", "button_fg": "#000000",
            "log_bg": "#ffffff", "log_fg": "#000000",
            "status_wait": "#0D47A1", "status_run": "#388E3C", "status_stop": "#D32F2F"
        }
        self.dark_theme = {
            "bg": "#212121", "fg": "#e0e0e0", "frame_bg": "#2c2c2c",
            "entry_bg": "#3c3c3c", "entry_fg": "#e0e0e0",
            "button_bg": "#424242", "button_fg": "#e0e0e0",
            "log_bg": "#1a1a1a", "log_fg": "#e0e0e0",
            "status_wait": "#64B5F6", "status_run": "#81C784", "status_stop": "#E57373"
        }

        self.style = ttk.Style(self.root)
        self.style.theme_use('clam')
        
        self.create_widgets()
        self.apply_theme()
        
        # --- [기능 추가] 창 닫기 이벤트 핸들러 ---
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def create_widgets(self):
        self.main_frame = tk.Frame(self.root, padx=10, pady=10)
        self.main_frame.pack(fill="both", expand=True)
        
        self.settings_frame = tk.LabelFrame(self.main_frame, text="거래 설정", padx=10, pady=10, relief=tk.GROOVE, borderwidth=1)
        self.settings_frame.pack(fill="x")

        # API 설정
        self.api_key_label = tk.Label(self.settings_frame, text="API Key:")
        self.api_key_label.grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self.api_key_entry = tk.Entry(self.settings_frame, width=40, show="*")
        self.api_key_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=5, pady=3)
        
        self.api_secret_label = tk.Label(self.settings_frame, text="Secret Key:")
        self.api_secret_label.grid(row=1, column=0, sticky="w", padx=5, pady=3)
        self.api_secret_entry = tk.Entry(self.settings_frame, width=40, show="*")
        self.api_secret_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=5, pady=3)

        self.param_labels = []
        params_texts = ["심볼:", "Timeframe:", "Trend Timeframe:", "손익비 (RR Ratio):", "고정 손실액 (USD):", "초기 자본금 (USD):", "수익 재투자 비율 (%):", "OB 진입 레벨:"]
        for i, text in enumerate(params_texts):
            label = tk.Label(self.settings_frame, text=text)
            label.grid(row=i+2, column=0, sticky="w", padx=5, pady=3)
            self.param_labels.append(label)

        self.symbol_var = tk.StringVar()
        symbols = ['ETC_USDT', 'BTC_USDT', 'ETH_USDT', 'XRP_USDT', 'SOL_USDT']
        self.symbol_menu = ttk.OptionMenu(self.settings_frame, self.symbol_var, symbols[0], *symbols)
        self.symbol_menu.grid(row=2, column=1, sticky="ew", padx=5, pady=3)

        timeframes = ['1m', '3m', '5m', '15m', '30m', '1h', '4h']
        self.timeframe_var = tk.StringVar(value='5m')
        self.timeframe_menu = ttk.OptionMenu(self.settings_frame, self.timeframe_var, timeframes[2], *timeframes)
        self.timeframe_menu.grid(row=3, column=1, sticky="ew", padx=5, pady=3)
        
        self.trend_timeframe_var = tk.StringVar(value='30m')
        self.trend_timeframe_menu = ttk.OptionMenu(self.settings_frame, self.trend_timeframe_var, timeframes[4], *timeframes)
        self.trend_timeframe_menu.grid(row=4, column=1, sticky="ew", padx=5, pady=3)

        self.rr_ratio_entry = tk.Entry(self.settings_frame); self.rr_ratio_entry.insert(0, "10.0")
        self.rr_ratio_entry.grid(row=5, column=1, sticky="ew", padx=5, pady=3)
        
        self.risk_usd_entry = tk.Entry(self.settings_frame); self.risk_usd_entry.insert(0, "5")
        self.risk_usd_entry.grid(row=6, column=1, sticky="ew", padx=5, pady=3)

        self.initial_capital_entry = tk.Entry(self.settings_frame); self.initial_capital_entry.insert(0, "1000")
        self.initial_capital_entry.grid(row=7, column=1, sticky="ew", padx=5, pady=3)

        self.reinvest_pct_entry = tk.Entry(self.settings_frame); self.reinvest_pct_entry.insert(0, "50")
        self.reinvest_pct_entry.grid(row=8, column=1, sticky="ew", padx=5, pady=3)
        
        self.ob_level_entry = tk.Entry(self.settings_frame); self.ob_level_entry.insert(0, "0.7")
        self.ob_level_entry.grid(row=9, column=1, sticky="ew", padx=5, pady=3)
        
        # 컨트롤 프레임
        self.control_frame = tk.Frame(self.main_frame)
        self.control_frame.pack(fill="x", pady=10)
        
        self.balance_label = tk.Label(self.control_frame, text="잔액: N/A")
        self.balance_label.pack(side="left")
        
        self.status_label = tk.Label(self.control_frame, text="상태: 대기중")
        self.status_label.pack(side="left", padx=20)
        
        self.start_button = tk.Button(self.control_frame, text="거래 시작", command=self.start_bot, fg="white", width=10, relief=tk.RAISED, borderwidth=2)
        self.start_button.pack(side="right", padx=5)
        self.stop_button = tk.Button(self.control_frame, text="거래 정지", command=self.stop_bot, state="disabled", fg="white", width=10, relief=tk.RAISED, borderwidth=2)
        self.stop_button.pack(side="right")
        
        # --- [기능 추가] 포지션 종료 버튼 ---
        self.close_pos_button = tk.Button(self.control_frame, text="포지션 종료", command=self.force_close_position, state="disabled", bg="#FFC107", fg="#000000", width=10, relief=tk.RAISED, borderwidth=2)
        self.close_pos_button.pack(side="right", padx=5)

        # 로그 프레임 및 버튼
        self.log_control_frame = tk.Frame(self.main_frame)
        self.log_control_frame.pack(fill="x")
        
        self.log_label = tk.Label(self.log_control_frame, text="로그")
        self.log_label.pack(side="left")
        
        self.clear_log_button = tk.Button(self.log_control_frame, text="로그 지우기", command=self.clear_log, width=10)
        self.clear_log_button.pack(side="right")
        
        self.theme_button = tk.Button(self.log_control_frame, text="테마 변경", command=self.toggle_theme, width=10)
        self.theme_button.pack(side="right", padx=5)
        
        self.alarm_on = tk.BooleanVar(value=True)
        self.alarm_check = tk.Checkbutton(self.log_control_frame, text="알람", var=self.alarm_on)
        self.alarm_check.pack(side="right", padx=5)
        
        self.log_text = scrolledtext.ScrolledText(self.main_frame, wrap=tk.WORD, height=15)
        self.log_text.pack(fill="both", expand=True, pady=(5,0))
        self.log_text.configure(state='disabled')

        self.root.after(100, self.process_queue)

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.apply_theme()

    def apply_theme(self):
        theme = self.dark_theme if self.is_dark_mode else self.light_theme
        
        self.root.configure(bg=theme["bg"])
        self.main_frame.configure(bg=theme["bg"])
        self.control_frame.configure(bg=theme["bg"])
        self.log_control_frame.configure(bg=theme["bg"])
        
        self.settings_frame.configure(bg=theme["frame_bg"], fg=theme["fg"], font=self.FONT_MAIN)
        self.api_key_label.configure(bg=theme["frame_bg"], fg=theme["fg"], font=self.FONT_MAIN)
        self.api_secret_label.configure(bg=theme["frame_bg"], fg=theme["fg"], font=self.FONT_MAIN)
        for label in self.param_labels:
            label.configure(bg=theme["frame_bg"], fg=theme["fg"], font=self.FONT_MAIN)

        entry_widgets = [self.api_key_entry, self.api_secret_entry, self.rr_ratio_entry, self.risk_usd_entry, self.initial_capital_entry, self.reinvest_pct_entry, self.ob_level_entry]
        for widget in entry_widgets:
            widget.configure(bg=theme["entry_bg"], fg=theme["entry_fg"], insertbackground=theme["fg"])

        self.alarm_check.configure(bg=theme["bg"], fg=theme["fg"], selectcolor=theme["bg"], activebackground=theme["bg"], font=self.FONT_MAIN)

        self.balance_label.configure(bg=theme["bg"], fg=theme["fg"], font=self.FONT_MAIN)
        self.status_label.configure(bg=theme["bg"], font=self.FONT_MAIN)
        self.log_label.configure(bg=theme["bg"], fg=theme["fg"], font=self.FONT_MAIN)
        
        self.theme_button.configure(bg=theme["button_bg"], fg=theme["button_fg"], font=("Helvetica", 9))
        self.clear_log_button.configure(bg=theme["button_bg"], fg=theme["button_fg"], font=("Helvetica", 9))
        
        if self.start_button['state'] == 'disabled':
            self.status_label.config(fg=theme["status_run"])
        else:
            self.status_label.config(text="상태: 대기중", fg=theme.get("status_wait", theme["fg"]))
            
        self.start_button.configure(bg="#4CAF50")
        self.stop_button.configure(bg="#f44336")

        self.log_text.configure(bg=theme["log_bg"], fg=theme["log_fg"], font=self.FONT_LOG)

        self.style.configure('TLabel', background=theme["frame_bg"], foreground=theme["fg"])
        self.style.configure('TMenubutton', background=theme["entry_bg"], foreground=theme["entry_fg"])

    def start_bot(self):
        api_key = self.api_key_entry.get()
        api_secret = self.api_secret_entry.get()
        if not api_key or not api_secret:
            messagebox.showerror("오류", "API Key와 Secret Key를 모두 입력해주세요.")
            return

        try:
            params = {
                'symbol': self.symbol_var.get(),
                'timeframe': self.timeframe_var.get(),
                'trend_timeframe': self.trend_timeframe_var.get(),
                'rr_ratio': float(self.rr_ratio_entry.get()),
                'risk_per_trade_usd': float(self.risk_usd_entry.get()),
                'initial_capital': float(self.initial_capital_entry.get()),
                'reinvestment_percent': float(self.reinvest_pct_entry.get()) / 100.0,
                'ob_entry_level': float(self.ob_level_entry.get()),
            }
        except ValueError:
            messagebox.showerror("입력 오류", "숫자 파라미터에 유효한 숫자를 입력하세요.")
            return
            
        self.bot = TradingBot(api_key, api_secret, params, self.msg_queue)
        self.bot_thread = threading.Thread(target=self.bot.run, daemon=True)
        self.bot_thread.start()

        for child in self.settings_frame.winfo_children():
            if hasattr(child, 'configure') and 'state' in child.configure():
                child.config(state="disabled")
        
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.close_pos_button.config(state="normal")
        theme = self.dark_theme if self.is_dark_mode else self.light_theme
        self.status_label.config(text="상태: 실행 중", fg=theme["status_run"])
        self.add_log("봇 스레드를 시작합니다.")

    def stop_bot(self):
        if self.bot:
            self.bot.stop()
        
        for child in self.settings_frame.winfo_children():
            if hasattr(child, 'configure') and 'state' in child.configure():
                child.config(state="normal")

        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self.close_pos_button.config(state="disabled")
        theme = self.dark_theme if self.is_dark_mode else self.light_theme
        self.status_label.config(text="상태: 정지됨", fg=theme.get("status_stop", theme["fg"]))
        self.add_log("사용자가 봇을 정지시켰습니다.")

    def force_close_position(self):
        """'포지션 즉시 종료' 버튼의 기능을 수행합니다."""
        if self.bot and self.bot.is_running:
            if messagebox.askyesno("포지션 종료 확인", "정말로 현재 포지션을 시장가로 종료하시겠습니까?"):
                # 봇의 메소드를 직접 호출하기 위해 별도 스레드 사용
                threading.Thread(target=self.bot.close_position_market, daemon=True).start()
        else:
            messagebox.showwarning("경고", "봇이 실행 중일 때만 포지션을 종료할 수 있습니다.")

    def clear_log(self):
        """로그 지우기 버튼의 기능을 수행합니다."""
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state='disabled')
        
    def on_closing(self):
        """창을 닫을 때 호출되는 함수입니다."""
        if self.bot and self.bot.is_running:
            if messagebox.askyesno("종료 확인", "봇이 실행 중입니다. 모든 포지션을 종료하고 프로그램을 닫으시겠습니까?"):
                self.add_log("프로그램 종료 중... 포지션을 정리합니다.")
                threading.Thread(target=self.bot.close_position_market, daemon=True).start()
                # 봇이 포지션을 정리할 시간을 잠시 줍니다.
                self.root.after(2000, self.root.destroy)
            else:
                return # 종료 취소
        else:
            self.root.destroy()

    def process_queue(self):
        try:
            while not self.msg_queue.empty():
                message = self.msg_queue.get_nowait()
                if message.startswith("LOG:"): self.add_log(message[5:])
                elif message.startswith("BALANCE:"): self.balance_label.config(text=f"잔액: {message[9:]}")
                elif message == "ALARM":
                    if self.alarm_on.get(): self.play_sound()
                elif message == "STOP_BOT": self.stop_bot()
        finally:
            self.root.after(100, self.process_queue)

    def add_log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] {message}\n"
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, log_message)
        self.log_text.configure(state='disabled')
        self.log_text.see(tk.END)

    def play_sound(self):
        def _play():
            try:
                sound_file = 'alarm.mp3'
                if os.path.exists(sound_file): playsound(sound_file)
                else: self.add_log(f"알람 경고: '{sound_file}' 파일을 찾을 수 없습니다.")
            except Exception as e:
                self.add_log(f"알람 재생 오류: {e}")
        sound_thread = threading.Thread(target=_play, daemon=True)
        sound_thread.start()

# -----------------------------------------------------------------------------
# 애플리케이션 실행
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()

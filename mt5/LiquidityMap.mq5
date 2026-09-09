//+------------------------------------------------------------------+
//|                                                  LiquidityMap.mq5 |
//|   Live buy-side / sell-side liquidity map for MetaTrader 5.       |
//|   Draws resting liquidity only - no entries, no signals, no SL/TP |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property link      "https://github.com/zalfenissmm-ops/zalfeni-sabri"
#property version   "1.00"
#property description "Live liquidity map: equal highs/lows, swing liquidity, PDH/PDL, PWH/PWL."
#property description "Shows what is still untapped, what is being raided right now, and what got swept."
#property description "Mapping tool only - it draws no entries and gives no trade signals."
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

//--- pool lifecycle
#define ST_LIVE     0   // untapped - liquidity still resting there
#define ST_RAIDING  1   // the forming bar is piercing it right now (provisional)
#define ST_SWEPT    2   // wick took it and price closed back = liquidity grabbed
#define ST_CLAIMED  3   // closed beyond it = taken and held, no longer a pool

//--- pool origin
#define TP_SWING    0
#define TP_DAY      1
#define TP_WEEK     2

//--- signal kinds
#define SIG_BUILD   0   // equal high/low forming on the live bar - instant
#define SIG_NEW     1   // a fractal confirmed a pool - RightBars behind by nature
#define SIG_RAID    2   // the live bar is piercing a pool - instant
#define SIG_SWEEP   3   // the raid closed back inside - confirmed on bar close

input group             "=== Detection ==="
input int    InpLeftBars       = 3;      // Swing: bars to the left
input int    InpRightBars      = 3;      // Swing: bars to the right (confirmation lag)
input int    InpMaxBarsBack    = 1500;   // History depth in bars (0 = all)
input int    InpAtrPeriod      = 14;     // ATR period (drives tolerance + distance)
input double InpEqTolATR       = 0.15;   // Equal-highs/lows tolerance, in ATR
input int    InpMinTouches     = 1;      // 1 = single swings too, 2 = equal highs/lows only
input int    InpMaxPools       = 12;     // Max pools drawn per side
input double InpMinScore       = 0.0;    // Hide pools scoring below this (0-100)

input group             "=== Higher timeframe liquidity ==="
input bool   InpShowPrevDay    = true;   // Previous day high/low (PDH/PDL)
input bool   InpShowPrevWeek   = true;   // Previous week high/low (PWH/PWL)
input ENUM_TIMEFRAMES InpHTF1  = PERIOD_H1;   // Higher timeframe 1 (CURRENT = off)
input ENUM_TIMEFRAMES InpHTF2  = PERIOD_H4;   // Higher timeframe 2 (CURRENT = off)
input ENUM_TIMEFRAMES InpHTF3  = PERIOD_D1;   // Higher timeframe 3 (CURRENT = off)
input int    InpHTFBars        = 600;    // Bars to scan on each higher timeframe
input int    InpHTFMaxPools    = 3;      // Max pools kept per side per timeframe

input group             "=== Display ==="
input color  InpBuySideColor   = clrDodgerBlue;  // Buy-side liquidity (BSL, above)
input color  InpSellSideColor  = clrOrangeRed;   // Sell-side liquidity (SSL, below)
input color  InpRaidColor      = clrGold;        // Being raided right now
input color  InpSweptColor     = clrDimGray;     // Already swept
input int    InpLineWidth      = 1;      // Line width
input bool   InpShowZoneBox    = false;  // Draw the pool as a band instead of a line
input bool   InpShowSwept      = true;   // Keep recently swept pools on the chart
input int    InpSweptKeepBars  = 30;     // ... for how many bars
input bool   InpShowLabels     = true;   // Text label on each pool
input int    InpLabelShiftBars = 2;      // Push labels this many bars right of price
input bool   InpShowPanel      = true;   // Corner panel with the nearest pools

input group             "=== Signals ==="
input bool   InpSigBuilding    = true;   // Liquidity BUILDING now (equal high/low forming) - tick level
input bool   InpSigNewPool     = true;   // New pool confirmed - lags RightBars, unavoidable
input bool   InpSigRaid        = true;   // Pool being raided right now - tick level
input bool   InpSigSweep       = true;   // Sweep completed on the bar close
input double InpSigMinScore    = 0.0;    // Only signal pools scoring at least this (0-100)
input bool   InpSigMarkers     = true;   // Leave a marker on the chart at each signal

input group             "=== Alert channels ==="
input bool   InpAlertPopup     = true;   // Popup window
input bool   InpAlertPush      = false;  // Push to the MT5 mobile app
input bool   InpAlertMail      = false;  // Email (set it up in Tools > Options > Email)
input bool   InpAlertSound     = false;  // Play a sound
input string InpSoundFile      = "alert.wav";

struct Pool
{
   int      side;          // +1 = buy-side (above), -1 = sell-side (below)
   int      type;          // TP_SWING / TP_DAY / TP_WEEK
   int      htf;           // 0 = this chart, else the ENUM_TIMEFRAMES it came from
   double   level;         // the extreme where stops rest
   double   lo;            // cluster low
   double   hi;            // cluster high
   double   trigger;       // price that counts as a raid of this pool
   int      touches;       // how many swings built the cluster
   int      state;         // ST_*
   int      bar_first;     // bar of the first swing in the cluster
   int      bar_confirm;   // bar at which the pool became known (no look-ahead)
   int      bar_end;       // bar that swept / claimed it
   double   score;         // 0..100 quality
   double   pull;          // score / (1 + distance in ATR) = live magnet strength
   bool     draw;
   bool     used;          // scratch flag for ranking
};

//--- one entry per signal already sent, so a rebuild never re-fires it
struct Fired
{
   int      kind;
   int      side;
   long     lvl;      // level in points - integer, so comparison is exact
   int      tag;      // touch count at fire time: x2 and x3 are different events
   datetime when;
   string   marker;
};

struct Pending
{
   int      kind;
   int      pool;
};

Pool     g_pools[];
Fired    g_fired[];
Pending  g_queue[];
bool     g_warmed     = false;  // suppresses the signal storm on first attach
bool     g_quiet      = false;  // higher-timeframe scans must not raise signals
ENUM_TIMEFRAMES g_htf[3];
int      g_htf_atr[3];
int      g_last_closed = -1;
string   g_sig_prefix = "LQS_";
int      g_atr_handle = INVALID_HANDLE;
double   g_atr        = 0.0;   // ATR of the last closed bar
double   g_tol        = 0.0;   // tolerance at the current bar
double   g_atrbuf[];           // per-bar ATR over the working window
int      g_atr_off    = 0;     // bar index that g_atrbuf[0] belongs to
datetime g_last_bar   = 0;
string   g_prefix     = "LQM_";

//+------------------------------------------------------------------+
int OnInit()
{
   if(InpLeftBars < 1 || InpRightBars < 1)
   {
      Print("LiquidityMap: LeftBars and RightBars must both be >= 1");
      return(INIT_PARAMETERS_INCORRECT);
   }
   if(InpAtrPeriod < 2)
   {
      Print("LiquidityMap: ATR period must be >= 2");
      return(INIT_PARAMETERS_INCORRECT);
   }

   g_atr_handle = iATR(_Symbol, _Period, InpAtrPeriod);
   if(g_atr_handle == INVALID_HANDLE)
   {
      Print("LiquidityMap: could not create the ATR handle");
      return(INIT_FAILED);
   }

   g_htf[0] = InpHTF1;
   g_htf[1] = InpHTF2;
   g_htf[2] = InpHTF3;
   for(int i = 0; i < 3; i++)
   {
      g_htf_atr[i] = INVALID_HANDLE;
      if(g_htf[i] == PERIOD_CURRENT)
         continue;
      if(PeriodSeconds(g_htf[i]) <= PeriodSeconds(_Period))
         continue;                       // only strictly higher timeframes
      g_htf_atr[i] = iATR(_Symbol, g_htf[i], InpAtrPeriod);
   }

   g_prefix     = StringFormat("LQM%d_%d_", InpLeftBars, InpRightBars);
   g_sig_prefix = StringFormat("LQS%d_%d_", InpLeftBars, InpRightBars);
   g_last_bar   = 0;
   g_warmed     = false;
   ArrayResize(g_fired, 0);
   IndicatorSetString(INDICATOR_SHORTNAME, StringFormat("LiquidityMap(%d/%d)", InpLeftBars, InpRightBars));
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, g_prefix);
   ObjectsDeleteAll(0, g_sig_prefix);
   if(g_atr_handle != INVALID_HANDLE)
      IndicatorRelease(g_atr_handle);
   for(int i = 0; i < 3; i++)
      if(g_htf_atr[i] != INVALID_HANDLE)
         IndicatorRelease(g_htf_atr[i]);
   ChartRedraw();
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   int need = InpLeftBars + InpRightBars + InpAtrPeriod + 5;
   if(rates_total < need)
      return(0);

   //--- force plain indexing: 0 = oldest, rates_total-1 = the forming bar
   ArraySetAsSeries(time,  false);
   ArraySetAsSeries(open,  false);
   ArraySetAsSeries(high,  false);
   ArraySetAsSeries(low,   false);
   ArraySetAsSeries(close, false);

   //--- ATR of the last closed bar: stable, does not jitter tick by tick
   double atr_buf[];
   ArraySetAsSeries(atr_buf, true);
   if(CopyBuffer(g_atr_handle, 0, 0, 2, atr_buf) < 2)
      return(prev_calculated);
   g_atr = atr_buf[1];
   if(g_atr <= 0.0)
      return(prev_calculated);
   g_tol = g_atr * InpEqTolATR;

   ArrayResize(g_queue, 0);

   bool changed = false;
   if(prev_calculated == 0 || time[rates_total - 1] != g_last_bar)
   {
      g_last_bar = time[rates_total - 1];
      BuildPools(rates_total, time, high, low, close);
      changed = true;
   }

   if(UpdateLive(rates_total, high, low))
      changed = true;

   //--- distances are re-measured every tick so the readout stays live;
   //--- the objects themselves are only rebuilt when something moved
   double price_now = close[rates_total - 1];
   ScorePools(rates_total, price_now);
   FlushSignals(time[rates_total - 1],
                (g_last_closed >= 0) ? time[g_last_closed] : time[rates_total - 1]);

   if(changed)
   {
      SelectPools(rates_total);
      Redraw(rates_total, time, price_now);
   }
   else if(InpShowPanel)
   {
      DrawPanel(price_now);
      ChartRedraw();
   }

   //--- the first pass replays the whole history; nothing from it is news,
   //--- so signals only start counting from the second pass onward
   g_warmed = true;
   return(rates_total);
}

//+------------------------------------------------------------------+
//| Signal engine.                                                    |
//|                                                                   |
//| BuildPools() wipes and rebuilds every pool on each new bar, so    |
//| pools carry no identity across rebuilds. Without a ledger the     |
//| same event would alert again every single bar. Keys are (kind,    |
//| side, level, touch count) with the level matched inside the       |
//| pool's own tolerance, so a merge that nudges the level by a tick  |
//| does not read as a fresh event - while x2 -> x3 on the same       |
//| level does, because that genuinely is new liquidity.              |
//+------------------------------------------------------------------+
bool AlreadyFired(const int kind, const int side, const long lvl,
                  const long tol_pts, const int tag)
{
   for(int i = ArraySize(g_fired) - 1; i >= 0; i--)
   {
      if(g_fired[i].kind != kind) continue;
      if(g_fired[i].side != side) continue;
      if(g_fired[i].tag  != tag)  continue;
      if(MathAbs(g_fired[i].lvl - lvl) <= tol_pts)
         return(true);
   }
   return(false);
}

void PruneFired()
{
   int n = ArraySize(g_fired);
   if(n <= 400)
      return;
   int drop = n - 300;
   for(int i = 0; i < drop; i++)
      if(g_fired[i].marker != "")
         ObjectDelete(0, g_fired[i].marker);
   for(int i = 0; i + drop < n; i++)
      g_fired[i] = g_fired[i + drop];
   ArrayResize(g_fired, n - drop);
}

void QueueSignal(const int kind, const int pool)
{
   if(!g_warmed || g_quiet)   // first pass, and HTF scans, stay quiet
      return;
   int n = ArraySize(g_queue);
   if(ArrayResize(g_queue, n + 1) != n + 1)
      return;
   g_queue[n].kind = kind;
   g_queue[n].pool = pool;
}

string SignalText(const int kind, const int k)
{
   string side_txt = (g_pools[k].side > 0) ? "BUY-SIDE" : "SELL-SIDE";
   string what;
   switch(kind)
   {
      case SIG_BUILD:
         what = StringFormat("liquidity BUILDING (equal %s x%d forming)",
                             (g_pools[k].side > 0 ? "highs" : "lows"), g_pools[k].touches);
         break;
      case SIG_NEW:
         what = (g_pools[k].touches > 1)
                ? StringFormat("pool CONFIRMED x%d", g_pools[k].touches)
                : "new pool";
         break;
      case SIG_RAID:  what = "being RAIDED right now";     break;
      case SIG_SWEEP: what = "SWEPT (wick took it)";       break;
      default:        what = "event";                      break;
   }
   return(StringFormat("%s %s | %s %s @ %s | score %.0f",
                       _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
                       side_txt, what,
                       DoubleToString(g_pools[k].level, _Digits),
                       g_pools[k].score));
}

void DrawMarker(const int kind, const int k, const datetime when, const string name)
{
   if(!InpSigMarkers)
      return;
   if(!ObjectCreate(0, name, OBJ_ARROW, 0, when, g_pools[k].level))
      return;

   int code = 159;                                   // BUILD: dot
   if(kind == SIG_NEW)   code = 158;                 // small square
   if(kind == SIG_RAID)  code = 161;                 // circle outline
   if(kind == SIG_SWEEP) code = 251;                 // cross

   color col = (kind == SIG_RAID || kind == SIG_SWEEP)
               ? InpRaidColor
               : ((g_pools[k].side > 0) ? InpBuySideColor : InpSellSideColor);

   ObjectSetInteger(0, name, OBJPROP_ARROWCODE, code);
   ObjectSetInteger(0, name, OBJPROP_COLOR, col);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_CENTER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, SignalText(kind, k));
}

//+------------------------------------------------------------------+
//| Send everything queued this pass. Scores are known by now, so the |
//| quality filter and the alert text both have real numbers in them. |
//+------------------------------------------------------------------+
void FlushSignals(const datetime t_live, const datetime t_closed)
{
   bool fired_any = false;
   int  q = ArraySize(g_queue);
   for(int i = 0; i < q; i++)
   {
      int kind = g_queue[i].kind;
      int k    = g_queue[i].pool;
      if(k < 0 || k >= ArraySize(g_pools))
         continue;

      if(kind == SIG_BUILD && !InpSigBuilding) continue;
      if(kind == SIG_NEW   && !InpSigNewPool)  continue;
      if(kind == SIG_RAID  && !InpSigRaid)     continue;
      if(kind == SIG_SWEEP && !InpSigSweep)    continue;
      if(g_pools[k].score < InpSigMinScore)    continue;

      double pt = (_Point > 0.0) ? _Point : 1.0;
      long lvl     = (long)MathRound(g_pools[k].level / pt);
      long tol_pts = (long)MathRound(MathAbs(g_pools[k].trigger - g_pools[k].level) / pt);
      if(tol_pts < 1)
         tol_pts = 1;

      if(AlreadyFired(kind, g_pools[k].side, lvl, tol_pts, g_pools[k].touches))
         continue;

      datetime when = (kind == SIG_BUILD || kind == SIG_RAID) ? t_live : t_closed;
      string   txt  = SignalText(kind, k);
      string   name = StringFormat("%s%d_%d_%I64d_%d", g_sig_prefix, kind,
                                   g_pools[k].side, lvl, g_pools[k].touches);

      int n = ArraySize(g_fired);
      if(ArrayResize(g_fired, n + 1) == n + 1)
      {
         g_fired[n].kind   = kind;
         g_fired[n].side   = g_pools[k].side;
         g_fired[n].lvl    = lvl;
         g_fired[n].tag    = g_pools[k].touches;
         g_fired[n].when   = when;
         g_fired[n].marker = InpSigMarkers ? name : "";
      }

      DrawMarker(kind, k, when, name);

      Print("LiquidityMap: ", txt);
      if(InpAlertPopup) Alert(txt);
      if(InpAlertPush)  SendNotification(txt);
      if(InpAlertMail)  SendMail("LiquidityMap signal", txt);
      if(InpAlertSound) PlaySound(InpSoundFile);
      fired_any = true;
   }
   ArrayResize(g_queue, 0);
   PruneFired();
   if(fired_any)
      ChartRedraw();      // a marker drawn on a quiet tick must show at once
}

//+------------------------------------------------------------------+
//| Tolerance measured with the volatility of THAT bar, not today's.  |
//| A 20-pip cluster is tight in a storm and sloppy in a dead session; |
//| using one global ATR would misjudge every older pool on the chart. |
//+------------------------------------------------------------------+
double TolAt(const int bar)
{
   int idx = bar - g_atr_off;
   if(idx >= 0 && idx < ArraySize(g_atrbuf) && g_atrbuf[idx] > 0.0)
      return(g_atrbuf[idx] * InpEqTolATR);
   return(g_tol);
}

//+------------------------------------------------------------------+
//| Rebuild every pool from scratch, walking bars in chronological    |
//| order so a pool's state only ever depends on bars that came       |
//| after it was confirmed. Nothing repaints from the future.         |
//+------------------------------------------------------------------+
void BuildPools(const int rates_total,
                const datetime &time[],
                const double &high[],
                const double &low[],
                const double &close[])
{
   ArrayResize(g_pools, 0);

   int first = (InpMaxBarsBack > 0) ? rates_total - InpMaxBarsBack : 0;
   if(first < InpLeftBars)
      first = InpLeftBars;

   int last_closed = rates_total - 2;      // the forming bar is handled by UpdateLive()
   g_last_closed = last_closed;
   if(last_closed <= first + InpRightBars)
      return;

   //--- ATR for every bar in the window, aligned so g_atrbuf[0] == bar `first`
   int want = rates_total - first;
   ArraySetAsSeries(g_atrbuf, false);
   g_atr_off = first;
   if(CopyBuffer(g_atr_handle, 0, 0, want, g_atrbuf) != want)
      ArrayResize(g_atrbuf, 0);           // TolAt() falls back to the current ATR

   bool daily  = (InpShowPrevDay  && _Period < PERIOD_D1);
   bool weekly = (InpShowPrevWeek && _Period < PERIOD_W1);

   MqlDateTime dt, prev_dt;
   TimeToStruct(time[first], prev_dt);

   double d_hi = high[first], d_lo = low[first];
   double w_hi = high[first], w_lo = low[first];
   //--- the first day/week in the window is cut off by the window itself,
   //--- so its high/low are not real period extremes - skip that one
   bool d_ready = false, w_ready = false;

   for(int i = first; i <= last_closed; i++)
   {
      //--- 1) settle raids / breaks of every pool that already existed
      ResolveBar(g_pools, i, high[i], low[i], close[i]);

      //--- 2) day / week rollover: the period that just ended leaves its
      //---    high and low behind as liquidity
      if(i > first)
      {
         TimeToStruct(time[i], dt);
         bool new_day  = (dt.day != prev_dt.day || dt.mon != prev_dt.mon || dt.year != prev_dt.year);
         bool new_week = (dt.day_of_week < prev_dt.day_of_week) ||
                         (time[i] - time[i - 1] > 2 * 24 * 60 * 60);

         if(new_week)
         {
            if(weekly && w_ready)
            {
               NewPool(g_pools, +1, TP_WEEK, w_hi, i, i, TolAt(i));
               NewPool(g_pools, -1, TP_WEEK, w_lo, i, i, TolAt(i));
            }
            w_ready = true;
            w_hi = high[i];
            w_lo = low[i];
         }
         else
         {
            w_hi = MathMax(w_hi, high[i]);
            w_lo = MathMin(w_lo, low[i]);
         }

         if(new_day)
         {
            if(daily && d_ready)
            {
               NewPool(g_pools, +1, TP_DAY, d_hi, i, i, TolAt(i));
               NewPool(g_pools, -1, TP_DAY, d_lo, i, i, TolAt(i));
            }
            d_ready = true;
            d_hi = high[i];
            d_lo = low[i];
         }
         else
         {
            d_hi = MathMax(d_hi, high[i]);
            d_lo = MathMin(d_lo, low[i]);
         }

         prev_dt = dt;
      }

      //--- 3) a fractal closes its right-hand window exactly at this bar,
      //---    so this is the first bar at which we are allowed to know it
      int s = i - InpRightBars;
      if(s >= first)
      {
         bool sw_high = true, sw_low = true;
         for(int j = s - InpLeftBars; j <= s + InpRightBars; j++)
         {
            if(j == s)
               continue;
            if(high[j] > high[s]) sw_high = false;
            if(low[j]  < low[s])  sw_low  = false;
         }
         double tol_s = TolAt(s);
         if(sw_high) AddSwing(g_pools, +1, high[s], s, i, tol_s);
         if(sw_low)  AddSwing(g_pools, -1, low[s],  s, i, tol_s);
      }
   }

   //--- and what the bigger timeframes are still holding above and below
   for(int t = 0; t < 3; t++)
      ScanHigherTF(g_htf[t], g_htf_atr[t], rates_total, time);
}

//+------------------------------------------------------------------+
string TFName(const int tf)
{
   string t = EnumToString((ENUM_TIMEFRAMES)tf);
   StringReplace(t, "PERIOD_", "");
   return(t);
}

//+------------------------------------------------------------------+
//| Chart bar that was open at time t. The chart's time[] is ascending |
//| so a binary search beats walking 1500 bars per imported pool.      |
//+------------------------------------------------------------------+
int ChartIndexOf(const datetime t, const int rates_total, const datetime &ctime[])
{
   if(t <= ctime[0])                return(0);
   if(t >= ctime[rates_total - 1])  return(rates_total - 1);
   int lo = 0, hi = rates_total - 1;
   while(lo < hi)
   {
      int mid = (lo + hi + 1) / 2;
      if(ctime[mid] <= t) lo = mid;
      else                hi = mid - 1;
   }
   return(lo);
}

//+------------------------------------------------------------------+
//| Read liquidity off a higher timeframe and project it onto this     |
//| chart. The sweep/claim walk runs on the higher timeframe's OWN     |
//| bars: an H4 level is taken when an H4 candle closes through it,    |
//| not when one M15 candle pokes past. Only untapped pools are        |
//| imported - a level H4 already ate is not resistance any more.      |
//+------------------------------------------------------------------+
void ScanHigherTF(const ENUM_TIMEFRAMES tf, const int atr_handle,
                  const int rates_total, const datetime &ctime[])
{
   if(tf == PERIOD_CURRENT || atr_handle == INVALID_HANDLE)
      return;
   if(PeriodSeconds(tf) <= PeriodSeconds(_Period))
      return;

   MqlRates r[];
   ArraySetAsSeries(r, false);
   int n = CopyRates(_Symbol, tf, 0, InpHTFBars, r);
   if(n < InpAtrPeriod + InpLeftBars + InpRightBars + 5)
      return;                       // history not loaded yet - try again next tick

   double atr[];
   ArraySetAsSeries(atr, false);
   if(CopyBuffer(atr_handle, 0, 0, n, atr) != n)
      return;

   int first       = InpLeftBars;
   int last_closed = n - 2;
   if(last_closed <= first + InpRightBars)
      return;

   Pool tmp[];
   ArrayResize(tmp, 0);

   g_quiet = true;                  // context, not a trigger: no alerts from here
   for(int i = first; i <= last_closed; i++)
   {
      ResolveBar(tmp, i, r[i].high, r[i].low, r[i].close);

      int sw = i - InpRightBars;
      if(sw < first)
         continue;

      bool sw_high = true, sw_low = true;
      for(int j = sw - InpLeftBars; j <= sw + InpRightBars; j++)
      {
         if(j == sw) continue;
         if(r[j].high > r[sw].high) sw_high = false;
         if(r[j].low  < r[sw].low)  sw_low  = false;
      }
      double tol = (atr[sw] > 0.0) ? atr[sw] * InpEqTolATR : g_tol;
      if(sw_high) AddSwing(tmp, +1, r[sw].high, sw, i, tol);
      if(sw_low)  AddSwing(tmp, -1, r[sw].low,  sw, i, tol);
   }
   g_quiet = false;

   //--- newest untapped levels first: those are the ones price is working on
   for(int side = 1; side >= -1; side -= 2)
   {
      int kept = 0;
      for(int k = ArraySize(tmp) - 1; k >= 0 && kept < InpHTFMaxPools; k--)
      {
         if(tmp[k].side != side || tmp[k].state != ST_LIVE)
            continue;

         int dst = ArraySize(g_pools);
         if(ArrayResize(g_pools, dst + 1) != dst + 1)
            return;

         g_pools[dst] = tmp[k];
         g_pools[dst].htf         = (int)tf;
         g_pools[dst].bar_first   = ChartIndexOf(r[tmp[k].bar_first].time, rates_total, ctime);
         g_pools[dst].bar_confirm = ChartIndexOf(r[tmp[k].bar_confirm].time, rates_total, ctime);
         if(g_pools[dst].bar_confirm > rates_total - 2)
            g_pools[dst].bar_confirm = rates_total - 2;   // let it go live immediately
         g_pools[dst].bar_end = -1;
         g_pools[dst].draw    = false;
         g_pools[dst].used    = false;
         kept++;
      }
   }
}

//+------------------------------------------------------------------+
//| One bar against every live pool.                                  |
//| Piercing the trigger and closing back  = swept (liquidity grab).  |
//| Closing beyond it                      = claimed (level is gone). |
//+------------------------------------------------------------------+
void ResolveBar(Pool &arr[], const int bar, const double h, const double l, const double c)
{
   int n = ArraySize(arr);
   for(int k = 0; k < n; k++)
   {
      if(arr[k].state != ST_LIVE)
         continue;
      if(arr[k].htf != 0)                // settled on its own timeframe already
         continue;
      if(arr[k].bar_confirm >= bar)      // not knowable yet at this bar
         continue;

      if(arr[k].side > 0)
      {
         if(h > arr[k].trigger)
         {
            arr[k].state   = (c > arr[k].trigger) ? ST_CLAIMED : ST_SWEPT;
            arr[k].bar_end = bar;
            if(arr[k].state == ST_SWEPT && bar == g_last_closed)
               QueueSignal(SIG_SWEEP, k);
         }
      }
      else
      {
         if(l < arr[k].trigger)
         {
            arr[k].state   = (c < arr[k].trigger) ? ST_CLAIMED : ST_SWEPT;
            arr[k].bar_end = bar;
            if(arr[k].state == ST_SWEPT && bar == g_last_closed)
               QueueSignal(SIG_SWEEP, k);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Add a swing, merging it into a still-live cluster when it sits    |
//| within tolerance: three equal highs are ONE strong pool, not two. |
//+------------------------------------------------------------------+
void AddSwing(Pool &arr[], const int side, const double price, const int bar_swing,
              const int bar_conf, const double tol)
{
   for(int k = ArraySize(arr) - 1; k >= 0; k--)
   {
      if(arr[k].type  != TP_SWING) continue;
      if(arr[k].side  != side)     continue;
      if(arr[k].state != ST_LIVE)  continue;
      if(MathAbs(arr[k].level - price) > tol) continue;

      arr[k].hi      = MathMax(arr[k].hi, price);
      arr[k].lo      = MathMin(arr[k].lo, price);
      arr[k].level   = (side > 0) ? arr[k].hi : arr[k].lo;
      arr[k].trigger = (side > 0) ? arr[k].hi + tol : arr[k].lo - tol;
      arr[k].touches++;
      arr[k].bar_confirm = bar_conf;
      if(bar_conf == g_last_closed)
         QueueSignal(SIG_NEW, k);      // equal high/low just became official
      return;
   }
   NewPool(arr, side, TP_SWING, price, bar_swing, bar_conf, tol);
}

//+------------------------------------------------------------------+
void NewPool(Pool &arr[], const int side, const int type, const double price,
             const int bar_first, const int bar_conf, const double tol)
{
   int n = ArraySize(arr);
   if(ArrayResize(arr, n + 1) != n + 1)
      return;

   arr[n].side        = side;
   arr[n].type        = type;
   arr[n].htf         = 0;
   arr[n].level       = price;
   arr[n].hi          = price;
   arr[n].lo          = price;
   arr[n].trigger     = (side > 0) ? price + tol : price - tol;
   arr[n].touches     = 1;
   arr[n].state       = ST_LIVE;
   arr[n].bar_first   = bar_first;
   arr[n].bar_confirm = bar_conf;
   arr[n].bar_end     = -1;
   arr[n].score       = 0.0;
   arr[n].pull        = 0.0;
   arr[n].draw        = false;
   arr[n].used        = false;

   if(bar_conf == g_last_closed)
      QueueSignal(SIG_NEW, n);
}

//+------------------------------------------------------------------+
//| The forming bar. A raid here is provisional - it is re-settled    |
//| into swept or claimed once the bar closes.                        |
//+------------------------------------------------------------------+
bool UpdateLive(const int rates_total, const double &high[], const double &low[])
{
   int  last    = rates_total - 1;
   bool changed = false;

   for(int k = 0; k < ArraySize(g_pools); k++)
   {
      if(g_pools[k].state != ST_LIVE && g_pools[k].state != ST_RAIDING)
         continue;
      if(g_pools[k].bar_confirm >= last)
         continue;

      //--- the pool's own equality band, both sides of the level
      double tolp = MathAbs(g_pools[k].trigger - g_pools[k].level);
      bool   raid, building;

      if(g_pools[k].side > 0)
      {
         raid     = (high[last] > g_pools[k].trigger);
         building = (!raid && high[last] >= g_pools[k].level - tolp);
      }
      else
      {
         raid     = (low[last] < g_pools[k].trigger);
         building = (!raid && low[last] <= g_pools[k].level + tolp);
      }

      int want = raid ? ST_RAIDING : ST_LIVE;
      if(g_pools[k].state != want)
      {
         g_pools[k].state = want;
         changed = true;
      }

      //--- price is back inside the band without breaking it: another equal
      //--- high/low is being printed, i.e. liquidity is stacking up right
      //--- now. This is the one formation event that needs no confirmation
      //--- lag - it is true the moment the tick arrives.
      if(raid)          QueueSignal(SIG_RAID,  k);
      else if(building) QueueSignal(SIG_BUILD, k);
   }
   return(changed);
}

//+------------------------------------------------------------------+
//| score = how much liquidity should be resting there (0-100)        |
//| pull  = how strong a magnet it is RIGHT NOW, distance-adjusted    |
//+------------------------------------------------------------------+
void ScorePools(const int rates_total, const double price_now)
{
   int last = rates_total - 1;

   for(int k = 0; k < ArraySize(g_pools); k++)
   {
      double s = 0.0;

      // stacked stops: every extra equal high/low is another wall of them
      s += MathMin(g_pools[k].touches, 4) * 10.0;

      // tightness: the closer the highs are to truly equal, the cleaner the pool
      double tol_k = TolAt(g_pools[k].bar_confirm);
      if(g_pools[k].touches > 1 && tol_k > 0.0)
         s += 20.0 * MathMax(0.0, 1.0 - (g_pools[k].hi - g_pools[k].lo) / tol_k);
      else
         s += 8.0;

      // higher-timeframe levels carry far more resting orders
      if(g_pools[k].type == TP_DAY)  s += 15.0;
      if(g_pools[k].type == TP_WEEK) s += 25.0;

      // a level read off a bigger timeframe scales with how much bigger it is:
      // 4x the chart = +12, 16x = +24, capped so it never fully drowns local structure
      if(g_pools[k].htf != 0)
      {
         double ratio = (double)PeriodSeconds((ENUM_TIMEFRAMES)g_pools[k].htf) /
                        (double)PeriodSeconds(_Period);
         if(ratio > 1.0)
            s += MathMin(35.0, 12.0 * MathLog(ratio) / MathLog(4.0));
      }

      // a pool that survives keeps collecting stops
      double age = (double)(last - g_pools[k].bar_first);
      s += 10.0 * MathMin(1.0, age / 100.0);

      if(s > 100.0) s = 100.0;
      g_pools[k].score = s;

      double dist_atr = MathAbs(g_pools[k].level - price_now) / g_atr;
      g_pools[k].pull = s / (1.0 + dist_atr);
   }
}

//+------------------------------------------------------------------+
//| Keep the chart readable: best pools per side by live pull, with   |
//| near-duplicate levels collapsed into the stronger one.            |
//+------------------------------------------------------------------+
void SelectPools(const int rates_total)
{
   int n = ArraySize(g_pools);
   for(int k = 0; k < n; k++)
   {
      g_pools[k].draw = false;
      g_pools[k].used = false;
   }

   for(int side = 1; side >= -1; side -= 2)
   {
      int drawn = 0;
      while(drawn < InpMaxPools)
      {
         int best = -1;
         for(int k = 0; k < n; k++)
         {
            if(g_pools[k].used)          continue;
            if(g_pools[k].side != side)  continue;
            if(g_pools[k].state != ST_LIVE && g_pools[k].state != ST_RAIDING) continue;
            if(g_pools[k].type == TP_SWING && g_pools[k].htf == 0 &&
               g_pools[k].touches < InpMinTouches) continue;
            if(g_pools[k].score < InpMinScore) continue;
            if(best < 0 || g_pools[k].pull > g_pools[best].pull)
               best = k;
         }
         if(best < 0)
            break;

         g_pools[best].used = true;

         bool dup = false;
         for(int k = 0; k < n; k++)
         {
            if(!g_pools[k].draw)        continue;
            if(g_pools[k].side != side) continue;
            if(MathAbs(g_pools[k].level - g_pools[best].level) <= g_tol)
            {
               dup = true;
               break;
            }
         }
         if(!dup)
         {
            g_pools[best].draw = true;
            drawn++;
         }
      }
   }

   if(InpShowSwept)
   {
      int last = rates_total - 1;
      for(int k = 0; k < n; k++)
         if(g_pools[k].state == ST_SWEPT && g_pools[k].bar_end >= 0 &&
            (last - g_pools[k].bar_end) <= InpSweptKeepBars)
            g_pools[k].draw = true;
   }
}

//+------------------------------------------------------------------+
void Redraw(const int rates_total, const datetime &time[], const double price_now)
{
   ObjectsDeleteAll(0, g_prefix);

   datetime t_now = time[rates_total - 1];
   datetime t_lbl = (datetime)(t_now + (long)PeriodSeconds() * InpLabelShiftBars);
   int idx = 0;

   for(int k = 0; k < ArraySize(g_pools); k++)
   {
      if(!g_pools[k].draw)
         continue;

      color           col   = (g_pools[k].side > 0) ? InpBuySideColor : InpSellSideColor;
      int             width = InpLineWidth;
      ENUM_LINE_STYLE style = STYLE_SOLID;
      string          tag   = "";

      if(g_pools[k].state == ST_RAIDING)
      {
         col   = InpRaidColor;
         width = InpLineWidth + 1;
         tag   = "  RAID";
      }
      else if(g_pools[k].state == ST_SWEPT)
      {
         col   = InpSweptColor;
         style = STYLE_DOT;
         tag   = "  swept";
      }

      datetime t1 = time[g_pools[k].bar_first];
      datetime t2 = (g_pools[k].state == ST_SWEPT) ? time[g_pools[k].bar_end] : t_now;
      bool     ray = (g_pools[k].state != ST_SWEPT);

      string side_tag = (g_pools[k].side > 0) ? "BSL" : "SSL";
      string type_tag = "";
      if(g_pools[k].type == TP_DAY)  type_tag = " PD";
      if(g_pools[k].type == TP_WEEK) type_tag = " PW";
      if(g_pools[k].htf != 0)
      {
         type_tag = " " + TFName(g_pools[k].htf);
         if(g_pools[k].state == ST_LIVE)
            width = InpLineWidth + 1;      // bigger timeframe, heavier line
      }
      double dist_atr = MathAbs(g_pools[k].level - price_now) / g_atr;

      string tip = StringFormat("%s%s  %s  |  touches %d  |  score %.0f  |  pull %.0f  |  %.2f ATR away",
                                side_tag, type_tag,
                                DoubleToString(g_pools[k].level, _Digits),
                                g_pools[k].touches, g_pools[k].score,
                                g_pools[k].pull, dist_atr);

      if(InpShowZoneBox)
      {
         double b1 = (g_pools[k].side > 0) ? g_pools[k].lo : g_pools[k].trigger;
         double b2 = (g_pools[k].side > 0) ? g_pools[k].trigger : g_pools[k].hi;
         string bx = g_prefix + "B" + IntegerToString(idx);
         if(ObjectCreate(0, bx, OBJ_RECTANGLE, 0, t1, b1, t2, b2))
         {
            ObjectSetInteger(0, bx, OBJPROP_COLOR, col);
            ObjectSetInteger(0, bx, OBJPROP_FILL, true);
            ObjectSetInteger(0, bx, OBJPROP_BACK, true);
            ObjectSetInteger(0, bx, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, bx, OBJPROP_HIDDEN, true);
            ObjectSetString(0, bx, OBJPROP_TOOLTIP, tip);
         }
      }

      string ln = g_prefix + "L" + IntegerToString(idx);
      if(ObjectCreate(0, ln, OBJ_TREND, 0, t1, g_pools[k].level, t2, g_pools[k].level))
      {
         ObjectSetInteger(0, ln, OBJPROP_COLOR, col);
         ObjectSetInteger(0, ln, OBJPROP_WIDTH, width);
         ObjectSetInteger(0, ln, OBJPROP_STYLE, style);
         ObjectSetInteger(0, ln, OBJPROP_RAY_RIGHT, ray);
         ObjectSetInteger(0, ln, OBJPROP_RAY_LEFT, false);
         ObjectSetInteger(0, ln, OBJPROP_BACK, true);
         ObjectSetInteger(0, ln, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, ln, OBJPROP_HIDDEN, true);
         ObjectSetString(0, ln, OBJPROP_TOOLTIP, tip);
      }

      if(InpShowLabels)
      {
         string tx = g_prefix + "T" + IntegerToString(idx);
         if(ObjectCreate(0, tx, OBJ_TEXT, 0, t_lbl, g_pools[k].level))
         {
            ObjectSetString(0, tx, OBJPROP_TEXT,
                            StringFormat("%s%s x%d  S%.0f  %.1fATR%s",
                                         side_tag, type_tag, g_pools[k].touches,
                                         g_pools[k].score, dist_atr, tag));
            ObjectSetString(0, tx, OBJPROP_FONT, "Arial");
            ObjectSetInteger(0, tx, OBJPROP_FONTSIZE, 7);
            ObjectSetInteger(0, tx, OBJPROP_COLOR, col);
            ObjectSetInteger(0, tx, OBJPROP_ANCHOR, ANCHOR_LEFT);
            ObjectSetInteger(0, tx, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, tx, OBJPROP_HIDDEN, true);
         }
      }

      idx++;
   }

   if(InpShowPanel)
      DrawPanel(price_now);

   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Corner readout: what is closest above, what is closest below,     |
//| and whether anything is being raided at this very moment.         |
//+------------------------------------------------------------------+
void DrawPanel(const double price_now)
{
   int best_up = -1, best_dn = -1, raiding = -1, best_htf = -1;

   for(int k = 0; k < ArraySize(g_pools); k++)
   {
      if(!g_pools[k].draw)
         continue;
      if(g_pools[k].state != ST_LIVE && g_pools[k].state != ST_RAIDING)
         continue;

      if(g_pools[k].state == ST_RAIDING && raiding < 0)
         raiding = k;

      if(g_pools[k].htf != 0)
         if(best_htf < 0 ||
            MathAbs(g_pools[k].level - price_now) < MathAbs(g_pools[best_htf].level - price_now))
            best_htf = k;

      if(g_pools[k].side > 0 && g_pools[k].level > price_now)
      {
         if(best_up < 0 || g_pools[k].level < g_pools[best_up].level)
            best_up = k;
      }
      if(g_pools[k].side < 0 && g_pools[k].level < price_now)
      {
         if(best_dn < 0 || g_pools[k].level > g_pools[best_dn].level)
            best_dn = k;
      }
   }

   string lines[5];
   color  cols[5];

   lines[0] = StringFormat("Liquidity map  %d/%d   ATR %s",
                           InpLeftBars, InpRightBars, DoubleToString(g_atr, _Digits));
   cols[0]  = clrSilver;

   if(best_up >= 0)
   {
      lines[1] = StringFormat("Nearest BSL  %s   +%.2f ATR   S%.0f x%d",
                              DoubleToString(g_pools[best_up].level, _Digits),
                              (g_pools[best_up].level - price_now) / g_atr,
                              g_pools[best_up].score, g_pools[best_up].touches);
      cols[1] = InpBuySideColor;
   }
   else
   {
      lines[1] = "Nearest BSL  -";
      cols[1]  = clrSilver;
   }

   if(best_dn >= 0)
   {
      lines[2] = StringFormat("Nearest SSL  %s   -%.2f ATR   S%.0f x%d",
                              DoubleToString(g_pools[best_dn].level, _Digits),
                              (price_now - g_pools[best_dn].level) / g_atr,
                              g_pools[best_dn].score, g_pools[best_dn].touches);
      cols[2] = InpSellSideColor;
   }
   else
   {
      lines[2] = "Nearest SSL  -";
      cols[2]  = clrSilver;
   }

   if(best_htf >= 0)
   {
      lines[3] = StringFormat("HTF %-4s     %s   %s%.2f ATR   S%.0f",
                              TFName(g_pools[best_htf].htf),
                              DoubleToString(g_pools[best_htf].level, _Digits),
                              (g_pools[best_htf].level > price_now ? "+" : "-"),
                              MathAbs(g_pools[best_htf].level - price_now) / g_atr,
                              g_pools[best_htf].score);
      cols[3]  = (g_pools[best_htf].side > 0) ? InpBuySideColor : InpSellSideColor;
   }
   else
   {
      lines[3] = "HTF          -";
      cols[3]  = clrSilver;
   }

   if(raiding >= 0)
   {
      lines[4] = StringFormat("RAID NOW     %s %s",
                              (g_pools[raiding].side > 0 ? "BSL" : "SSL"),
                              DoubleToString(g_pools[raiding].level, _Digits));
      cols[4]  = InpRaidColor;
   }
   else
   {
      lines[4] = "RAID NOW     -";
      cols[4]  = clrSilver;
   }

   for(int i = 0; i < 5; i++)
   {
      string nm = g_prefix + "P" + IntegerToString(i);
      if(ObjectFind(0, nm) < 0)
      {
         if(!ObjectCreate(0, nm, OBJ_LABEL, 0, 0, 0))
            continue;
         ObjectSetInteger(0, nm, OBJPROP_CORNER, CORNER_LEFT_UPPER);
         ObjectSetInteger(0, nm, OBJPROP_XDISTANCE, 10);
         ObjectSetInteger(0, nm, OBJPROP_YDISTANCE, 18 + i * 14);
         ObjectSetInteger(0, nm, OBJPROP_FONTSIZE, 8);
         ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, nm, OBJPROP_HIDDEN, true);
         ObjectSetString(0, nm, OBJPROP_FONT, "Consolas");
      }
      ObjectSetInteger(0, nm, OBJPROP_COLOR, cols[i]);
      ObjectSetString(0, nm, OBJPROP_TEXT, lines[i]);
   }
}
//+------------------------------------------------------------------+

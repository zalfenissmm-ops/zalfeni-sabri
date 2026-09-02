//+------------------------------------------------------------------+
//|                                             SR_BreakSignal.mq5   |
//|          Support & Resistance zones + confirmed break signal      |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property link      "https://github.com/zalfenissmm-ops/zalfeni-sabri"
#property version   "1.00"
#property description "Builds support/resistance ZONES by clustering swing pivots and"
#property description "scores each zone (touches, reaction in ATR, recency, flip, round number)."
#property description "Signals only CONFIRMED breaks and marks false breaks (liquidity sweeps)."

#property indicator_chart_window
#property indicator_buffers 7
#property indicator_plots   7

#property indicator_label1  "Break Up"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrLime
#property indicator_width1  3

#property indicator_label2  "Break Down"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrRed
#property indicator_width2  3

#property indicator_label3  "False Break Up"
#property indicator_type3   DRAW_ARROW
#property indicator_color3  clrOrange
#property indicator_width3  2

#property indicator_label4  "False Break Down"
#property indicator_type4   DRAW_ARROW
#property indicator_color4  clrAqua
#property indicator_width4  2

#property indicator_label5  "Nearest Support"
#property indicator_type5   DRAW_NONE

#property indicator_label6  "Nearest Resistance"
#property indicator_type6   DRAW_NONE

#property indicator_label7  "Signal"
#property indicator_type7   DRAW_NONE

//--- signal mode
enum ENUM_SR_MODE
  {
   SR_MODE_BREAK  = 0,  // Break only (earlier entry)
   SR_MODE_RETEST = 1   // Break + retest (safer entry)
  };

input group             "=== Zone detection ==="
input int    InpLookback       = 600;    // Bars to analyse
input int    InpSwing          = 3;      // Swing (pivot) window
input int    InpATRPeriod      = 14;     // ATR period
input double InpZoneWidthATR   = 0.5;    // Zone width (x ATR)
input int    InpMinTouches     = 2;      // Min touches for a valid zone
input double InpMinScore       = 50.0;   // Min zone score (0-100)
input int    InpMaxZones       = 6;      // Max zones kept

input group             "=== Break confirmation ==="
input ENUM_SR_MODE InpSignalMode = SR_MODE_BREAK; // Signal mode
input int    InpConfirmBars    = 2;      // Closes needed beyond the zone
input double InpBreakBufferATR = 0.25;   // Close must clear the zone by (x ATR)
input double InpMinBodyRatio   = 0.50;   // Min body/range of the break candle
input double InpImpulseATR     = 1.00;   // Min break candle range (x ATR)
input bool   InpUseVolume      = true;   // Require volume expansion
input double InpVolumeMult     = 1.20;   // Break volume vs average
input int    InpVolumeAvgBars  = 20;     // Bars for the volume average
input int    InpRetestBars     = 20;     // Max bars to wait for the retest
input int    InpFakeBreakBars  = 3;      // Bars within which a break is voided

input group             "=== Display & alerts ==="
input bool   InpDrawZones      = true;   // Draw zone rectangles
input color  InpSupportColor   = clrSeaGreen;   // Support colour
input color  InpResistColor    = clrFireBrick;  // Resistance colour
input color  InpFlipColor      = clrGoldenrod;  // Flip zone colour
input bool   InpAlertPopup     = true;   // Popup alert
input bool   InpAlertPush      = false;  // Push notification
input bool   InpAlertSound     = false;  // Sound alert

//--- score weights (must sum to 1.0)
#define W_TOUCHES   0.30
#define W_REACTION  0.25
#define W_RECENCY   0.20
#define W_FLIP      0.15
#define W_ROUND     0.10

#define TOUCHES_FOR_FULL_SCORE  5
#define ATR_MOVE_FOR_FULL_SCORE 3.0
#define REACTION_LOOKAHEAD      10

//--- signal codes written into the Signal buffer
#define SIG_BREAK_UP    1.0
#define SIG_BREAK_DOWN -1.0
#define SIG_FAKE_UP     2.0
#define SIG_FAKE_DOWN  -2.0

//--- buffers
double BufBreakUp[];
double BufBreakDown[];
double BufFakeUp[];
double BufFakeDown[];
double BufSupport[];
double BufResist[];
double BufSignal[];

//--- working data
double   g_atr[];
datetime g_lastAlertTime = 0;
const string OBJ_PREFIX = "SRB_";

struct SRPivot
  {
   int      bar;      // bar index (chronological)
   double   price;
   int      kind;     // +1 = swing high, -1 = swing low
  };

struct SRZone
  {
   double   bottom;
   double   top;
   double   center;
   int      touches;
   int      firstTouch;
   int      lastTouch;
   bool     flipped;
   double   reaction;   // average reaction in ATR
   double   score;      // 0..100
  };

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpSwing < 1 || InpConfirmBars < 1 || InpATRPeriod < 2 || InpMinTouches < 1)
     {
      Print("SR_BreakSignal: Swing, ConfirmBars, MinTouches must be >= 1 and ATRPeriod >= 2.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpLookback < InpSwing * 4 + InpATRPeriod)
     {
      Print("SR_BreakSignal: Lookback is too small for the chosen Swing/ATR period.");
      return(INIT_PARAMETERS_INCORRECT);
     }

   SetIndexBuffer(0, BufBreakUp,   INDICATOR_DATA);
   SetIndexBuffer(1, BufBreakDown, INDICATOR_DATA);
   SetIndexBuffer(2, BufFakeUp,    INDICATOR_DATA);
   SetIndexBuffer(3, BufFakeDown,  INDICATOR_DATA);
   SetIndexBuffer(4, BufSupport,   INDICATOR_DATA);
   SetIndexBuffer(5, BufResist,    INDICATOR_DATA);
   SetIndexBuffer(6, BufSignal,    INDICATOR_DATA);

   PlotIndexSetInteger(0, PLOT_ARROW, 233);   // up arrow
   PlotIndexSetInteger(1, PLOT_ARROW, 234);   // down arrow
   PlotIndexSetInteger(2, PLOT_ARROW, 251);   // cross
   PlotIndexSetInteger(3, PLOT_ARROW, 251);   // cross

   for(int i = 0; i < 6; i++)
      PlotIndexSetDouble(i, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(6, PLOT_EMPTY_VALUE, 0.0);

   IndicatorSetString(INDICATOR_SHORTNAME, "S/R Break (" + IntegerToString(InpSwing) + ")");
   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, OBJ_PREFIX);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| ATR (simple average of true range) for every bar.                 |
//+------------------------------------------------------------------+
void ComputeATR(const double &high[], const double &low[], const double &close[],
                const int rates_total, const int period)
  {
   ArrayResize(g_atr, rates_total);
   double tr[];
   ArrayResize(tr, rates_total);

   for(int i = 0; i < rates_total; i++)
     {
      if(i == 0)
         tr[i] = high[0] - low[0];
      else
         tr[i] = MathMax(high[i] - low[i],
                         MathMax(MathAbs(high[i] - close[i - 1]),
                                 MathAbs(low[i] - close[i - 1])));
     }

   double sum = 0.0;
   for(int i = 0; i < rates_total; i++)
     {
      sum += tr[i];
      if(i >= period)
         sum -= tr[i - period];
      int n = (i + 1 < period) ? i + 1 : period;
      g_atr[i] = sum / n;
     }
  }

//+------------------------------------------------------------------+
//| Fractal swing highs/lows — the raw candidates for S/R.            |
//+------------------------------------------------------------------+
int FindPivots(const double &high[], const double &low[],
               const int from, const int to, const int w, SRPivot &out[])
  {
   ArrayResize(out, (to - from + 1) * 2);
   int n = 0;

   for(int i = from + w; i <= to - w; i++)
     {
      bool isHigh = true;
      bool isLow  = true;
      for(int k = i - w; k <= i + w; k++)
        {
         if(high[k] > high[i]) isHigh = false;
         if(low[k]  < low[i])  isLow  = false;
        }
      if(isHigh)
        {
         out[n].bar = i; out[n].price = high[i]; out[n].kind = 1; n++;
        }
      if(isLow)
        {
         out[n].bar = i; out[n].price = low[i]; out[n].kind = -1; n++;
        }
     }

   ArrayResize(out, n);
   return n;
  }

//+------------------------------------------------------------------+
//| How far price ran away from a pivot, in ATR units. A level price  |
//| barely bounced off is not a level.                                |
//+------------------------------------------------------------------+
double ReactionATR(const SRPivot &p, const int upToBar,
                   const double &high[], const double &low[])
  {
   double a = g_atr[p.bar];
   if(a <= 0.0)
      return 0.0;

   int end = p.bar + REACTION_LOOKAHEAD;
   if(end > upToBar) end = upToBar;
   if(end <= p.bar) return 0.0;

   double move = 0.0;
   if(p.kind > 0)
     {
      double lo = low[p.bar + 1];
      for(int k = p.bar + 2; k <= end; k++)
         lo = MathMin(lo, low[k]);
      move = p.price - lo;
     }
   else
     {
      double hi = high[p.bar + 1];
      for(int k = p.bar + 2; k <= end; k++)
         hi = MathMax(hi, high[k]);
      move = hi - p.price;
     }
   return MathMax(move, 0.0) / a;
  }

//+------------------------------------------------------------------+
//| Distance to the nearest psychological round number, 0..1.         |
//| The step scales with price: 0.01 for 1.2345, 10 for 2000.         |
//+------------------------------------------------------------------+
double RoundNumberScore(const double price)
  {
   if(price <= 0.0)
      return 0.0;
   double step = MathPow(10.0, MathFloor(MathLog10(price)) - 2.0);
   if(step <= 0.0)
      return 0.0;
   double nearest = MathRound(price / step) * step;
   return MathMax(0.0, 1.0 - MathAbs(price - nearest) / (step / 2.0));
  }

//+------------------------------------------------------------------+
//| Sort pivots by price (insertion sort — the array is small).       |
//+------------------------------------------------------------------+
void SortByPrice(SRPivot &arr[], const int count)
  {
   for(int i = 1; i < count; i++)
     {
      SRPivot key = arr[i];
      int j = i - 1;
      while(j >= 0 && arr[j].price > key.price)
        {
         arr[j + 1] = arr[j];
         j--;
        }
      arr[j + 1] = key;
     }
  }

//+------------------------------------------------------------------+
//| Build the scored zones from every pivot confirmed up to `upToBar`.|
//| Highs and lows are clustered together on purpose: broken          |
//| resistance becomes support, and both touches belong to one zone.  |
//+------------------------------------------------------------------+
int BuildZones(const SRPivot &pivots[], const int pivotCount, const int upToBar,
               const int scanStart, const double &high[], const double &low[],
               SRZone &zones[])
  {
   ArrayResize(zones, 0);
   if(pivotCount < InpMinTouches)
      return 0;

   SRPivot sorted[];
   ArrayResize(sorted, pivotCount);
   for(int i = 0; i < pivotCount; i++)
      sorted[i] = pivots[i];
   SortByPrice(sorted, pivotCount);

   double band = InpZoneWidthATR * g_atr[upToBar];
   if(band <= 0.0)
      band = 10 * _Point;

   ArrayResize(zones, pivotCount);
   int zoneCount = 0;
   int clusterStart = 0;

   for(int i = 1; i <= pivotCount; i++)
     {
      bool endOfCluster = true;
      if(i < pivotCount)
        {
         // joins the cluster if close to the previous pivot AND the whole
         // cluster stays under two bands wide (no drifting "zones")
         bool nearPrevious = (sorted[i].price - sorted[i - 1].price) <= band;
         bool staysNarrow  = (sorted[i].price - sorted[clusterStart].price) <= 2.0 * band;
         endOfCluster = !(nearPrevious && staysNarrow);
        }

      if(!endOfCluster)
         continue;

      int members = i - clusterStart;
      if(members >= InpMinTouches)
        {
         SRZone z;
         z.bottom     = sorted[clusterStart].price;
         z.top        = sorted[i - 1].price;
         z.touches    = members;
         z.firstTouch = sorted[clusterStart].bar;
         z.lastTouch  = sorted[clusterStart].bar;

         double sumPrice = 0.0;
         double sumReact = 0.0;
         bool hasHigh = false;
         bool hasLow  = false;

         for(int k = clusterStart; k < i; k++)
           {
            sumPrice += sorted[k].price;
            sumReact += ReactionATR(sorted[k], upToBar, high, low);
            if(sorted[k].kind > 0) hasHigh = true;
            else                   hasLow  = true;
            if(sorted[k].bar < z.firstTouch) z.firstTouch = sorted[k].bar;
            if(sorted[k].bar > z.lastTouch)  z.lastTouch  = sorted[k].bar;
           }

         z.center   = sumPrice / members;
         z.reaction = sumReact / members;
         z.flipped  = (hasHigh && hasLow);

         // support/resistance is an area, never a line
         if(z.top - z.bottom < band)
           {
            z.bottom = z.center - band / 2.0;
            z.top    = z.center + band / 2.0;
           }

         int    counted     = (members < TOUCHES_FOR_FULL_SCORE) ? members : TOUCHES_FOR_FULL_SCORE;
         double touchPart   = (double)counted / (double)TOUCHES_FOR_FULL_SCORE;
         double reactPart   = MathMin(z.reaction / ATR_MOVE_FOR_FULL_SCORE, 1.0);
         double span        = (double)(upToBar - scanStart);
         double recencyPart = (span > 0.0) ? (double)(z.lastTouch - scanStart) / span : 0.0;
         double flipPart    = z.flipped ? 1.0 : 0.0;
         double roundPart   = RoundNumberScore(z.center);

         z.score = 100.0 * (W_TOUCHES  * touchPart +
                            W_REACTION * reactPart +
                            W_RECENCY  * recencyPart +
                            W_FLIP     * flipPart +
                            W_ROUND    * roundPart);

         if(z.score >= InpMinScore)
           {
            zones[zoneCount] = z;
            zoneCount++;
           }
        }
      clusterStart = i;
     }

   ArrayResize(zones, zoneCount);

   // keep the strongest zones only
   for(int i = 1; i < zoneCount; i++)
     {
      SRZone key = zones[i];
      int j = i - 1;
      while(j >= 0 && zones[j].score < key.score)
        {
         zones[j + 1] = zones[j];
         j--;
        }
      zones[j + 1] = key;
     }
   if(zoneCount > InpMaxZones)
     {
      zoneCount = InpMaxZones;
      ArrayResize(zones, zoneCount);
     }
   return zoneCount;
  }

//+------------------------------------------------------------------+
//| Volume expansion on the break candle.                             |
//+------------------------------------------------------------------+
bool VolumeExpanded(const int b, const long &tick_volume[])
  {
   if(!InpUseVolume)
      return true;
   int n = (InpVolumeAvgBars < b) ? InpVolumeAvgBars : b;
   if(n < 5)
      return true;                    // not enough history: don't block the signal
   double sum = 0.0;
   for(int k = b - n; k < b; k++)
      sum += (double)tick_volume[k];
   double avg = sum / n;
   if(avg <= 0.0)
      return true;
   return ((double)tick_volume[b] >= InpVolumeMult * avg);
  }

//+------------------------------------------------------------------+
//| A real break: the candle CLOSES beyond the zone by a buffer, with |
//| a real body, an impulsive range, volume, and `ConfirmBars` closes |
//| that hold outside. `dir` = +1 up, -1 down. Returns the break bar  |
//| index in `breakBar`, or -1.                                       |
//+------------------------------------------------------------------+
bool IsConfirmedBreak(const int i, const SRZone &z, const int dir,
                      const double &open[], const double &high[],
                      const double &low[], const double &close[],
                      const long &tick_volume[])
  {
   int b = i - InpConfirmBars + 1;         // the candle that broke out
   if(b < 1)
      return false;

   double buffer = InpBreakBufferATR * g_atr[b];
   double edge   = (dir > 0) ? z.top : z.bottom;

   // price must have been on the other side before the break
   if(dir > 0 && close[b - 1] > edge) return false;
   if(dir < 0 && close[b - 1] < edge) return false;

   // the break candle must push in the break direction
   if(dir > 0 && close[b] <= open[b]) return false;
   if(dir < 0 && close[b] >= open[b]) return false;

   // every bar from the break to now must hold beyond the zone
   for(int k = b; k <= i; k++)
     {
      if(dir > 0 && close[k] <= edge + buffer) return false;
      if(dir < 0 && close[k] >= edge - buffer) return false;
     }

   double range = high[b] - low[b];
   if(range <= 0.0)
      return false;
   if(MathAbs(close[b] - open[b]) / range < InpMinBodyRatio)   // no doji / long wick
      return false;
   if(range < InpImpulseATR * g_atr[b])                        // must be impulsive
      return false;

   return VolumeExpanded(b, tick_volume);
  }

//+------------------------------------------------------------------+
//| Break + retest: after a confirmed break, price comes back to the  |
//| broken zone and gets rejected, closing beyond it again.           |
//+------------------------------------------------------------------+
bool IsBreakRetest(const int i, const SRZone &z, const int dir,
                   const double &open[], const double &high[],
                   const double &low[], const double &close[],
                   const long &tick_volume[])
  {
   double edge   = (dir > 0) ? z.top : z.bottom;
   double buffer = InpBreakBufferATR * g_atr[i];

   // the retest candle must dip into the zone and close back outside it
   if(dir > 0)
     {
      if(low[i] > edge + buffer)      return false;
      if(close[i] <= edge)            return false;
      if(close[i] <= open[i])         return false;
     }
   else
     {
      if(high[i] < edge - buffer)     return false;
      if(close[i] >= edge)            return false;
      if(close[i] >= open[i])         return false;
     }

   // a confirmed break must have happened recently, but not on this bar
   int from = i - InpRetestBars;
   if(from < 1) from = 1;
   for(int c = i - 1; c >= from; c--)
      if(IsConfirmedBreak(c, z, dir, open, high, low, close, tick_volume))
         return true;
   return false;
  }

//+------------------------------------------------------------------+
//| False break (liquidity sweep): either a wick that pierces the     |
//| zone and closes back inside, or a close beyond the zone that is   |
//| given back within `InpFakeBreakBars` bars.                        |
//+------------------------------------------------------------------+
bool IsFalseBreak(const int i, const SRZone &z, const int dir,
                  const double &high[], const double &low[], const double &close[])
  {
   if(i < 1)
      return false;

   double edge   = (dir > 0) ? z.top : z.bottom;
   double buffer = InpBreakBufferATR * g_atr[i];

   // price must have been on the near side of the zone before the sweep,
   // otherwise a bar merely approaching the zone would look like a failed break
   if(dir > 0 && close[i - 1] > edge) return false;
   if(dir < 0 && close[i - 1] < edge) return false;

   if(dir > 0)
     {
      if(close[i] >= edge)
         return false;
      if(high[i] > edge + buffer)                 // pierced and closed back inside
         return true;
      int from = i - InpFakeBreakBars;
      if(from < 1) from = 1;
      for(int b = from; b < i; b++)
         if(close[b] > edge && close[b - 1] <= edge)
            return true;
     }
   else
     {
      if(close[i] <= edge)
         return false;
      if(low[i] < edge - buffer)
         return true;
      int from = i - InpFakeBreakBars;
      if(from < 1) from = 1;
      for(int b = from; b < i; b++)
         if(close[b] < edge && close[b - 1] >= edge)
            return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
void DrawZones(const SRZone &zones[], const int zoneCount,
               const datetime &time[], const int rates_total, const double lastClose)
  {
   ObjectsDeleteAll(0, OBJ_PREFIX);
   if(!InpDrawZones)
      return;

   datetime rightEdge = time[rates_total - 1] + (datetime)(PeriodSeconds() * 10);

   for(int z = 0; z < zoneCount; z++)
     {
      string role = "S/R";
      color  clr  = InpFlipColor;
      if(zones[z].flipped)
        {
         role = "FLIP";
        }
      else
         if(zones[z].top < lastClose)
           {
            role = "SUP";
            clr  = InpSupportColor;
           }
         else
            if(zones[z].bottom > lastClose)
              {
               role = "RES";
               clr  = InpResistColor;
              }

      string rect = OBJ_PREFIX + "Z" + IntegerToString(z);
      ObjectCreate(0, rect, OBJ_RECTANGLE, 0,
                   time[zones[z].firstTouch], zones[z].top, rightEdge, zones[z].bottom);
      ObjectSetInteger(0, rect, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, rect, OBJPROP_FILL, true);
      ObjectSetInteger(0, rect, OBJPROP_BACK, true);
      ObjectSetInteger(0, rect, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, rect, OBJPROP_HIDDEN, true);

      string label = OBJ_PREFIX + "T" + IntegerToString(z);
      ObjectCreate(0, label, OBJ_TEXT, 0, rightEdge, zones[z].center);
      ObjectSetString(0, label, OBJPROP_TEXT,
                      StringFormat("%s %.0f | %d touches | %.1fxATR", role, zones[z].score,
                                   zones[z].touches, zones[z].reaction));
      ObjectSetString(0, label, OBJPROP_FONT, "Arial");
      ObjectSetInteger(0, label, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, label, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, label, OBJPROP_ANCHOR, ANCHOR_LEFT);
      ObjectSetInteger(0, label, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, label, OBJPROP_HIDDEN, true);
     }
   ChartRedraw();
  }

//+------------------------------------------------------------------+
void RaiseAlert(const double signal, const datetime barTime, const double price)
  {
   string what = "";
   if(signal == SIG_BREAK_UP)        what = "REAL BREAK UP — resistance broken";
   else if(signal == SIG_BREAK_DOWN) what = "REAL BREAK DOWN — support broken";
   else if(signal == SIG_FAKE_UP)    what = "FALSE BREAK UP — sweep, bearish rejection";
   else if(signal == SIG_FAKE_DOWN)  what = "FALSE BREAK DOWN — sweep, bullish rejection";
   else return;

   string msg = StringFormat("%s %s: %s @ %s", _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
                             what, DoubleToString(price, _Digits));

   if(InpAlertPopup) Alert(msg);
   if(InpAlertPush)  SendNotification(msg);
   if(InpAlertSound) PlaySound("alert.wav");
   Print(msg, "  bar time: ", TimeToString(barTime));
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
   if(rates_total < 2)
      return 0;

   ArraySetAsSeries(time, false);
   ArraySetAsSeries(open, false);
   ArraySetAsSeries(high, false);
   ArraySetAsSeries(low, false);
   ArraySetAsSeries(close, false);
   ArraySetAsSeries(tick_volume, false);
   ArraySetAsSeries(BufBreakUp, false);
   ArraySetAsSeries(BufBreakDown, false);
   ArraySetAsSeries(BufFakeUp, false);
   ArraySetAsSeries(BufFakeDown, false);
   ArraySetAsSeries(BufSupport, false);
   ArraySetAsSeries(BufResist, false);
   ArraySetAsSeries(BufSignal, false);

   // everything is evaluated on closed candles, so one pass per new bar is enough
   static datetime lastBarTime = 0;
   if(prev_calculated > 0 && time[rates_total - 1] == lastBarTime)
      return rates_total;
   bool firstPass = (lastBarTime == 0);
   lastBarTime = time[rates_total - 1];

   // cleared before any early exit, so a short history never leaves stale arrows
   for(int i = 0; i < rates_total; i++)
     {
      BufBreakUp[i]   = EMPTY_VALUE;
      BufBreakDown[i] = EMPTY_VALUE;
      BufFakeUp[i]    = EMPTY_VALUE;
      BufFakeDown[i]  = EMPTY_VALUE;
      BufSupport[i]   = EMPTY_VALUE;
      BufResist[i]    = EMPTY_VALUE;
      BufSignal[i]    = 0.0;
     }

   if(rates_total < InpSwing * 4 + InpATRPeriod + 10)
      return 0;

   ComputeATR(high, low, close, rates_total, InpATRPeriod);

   int scanStart  = rates_total - InpLookback;
   if(scanStart < 0)
      scanStart = 0;
   int lastClosed = rates_total - 2;                 // never signal on the forming bar
   if(lastClosed <= scanStart + InpSwing * 2)
      return rates_total;

   SRPivot pivots[];
   int pivotCount = FindPivots(high, low, scanStart, lastClosed, InpSwing, pivots);
   if(pivotCount == 0)
      return rates_total;

   SRPivot active[];
   ArrayResize(active, pivotCount);
   int activeCount = 0;
   int nextPivot   = 0;
   bool needRebuild = false;

   SRZone zones[];
   int zoneCount = 0;

   // walk forward: a zone is only known once its pivots are confirmed, so the
   // historical arrows use no information that was unavailable at the time
   for(int i = scanStart + InpSwing; i <= lastClosed; i++)
     {
      while(nextPivot < pivotCount && pivots[nextPivot].bar + InpSwing <= i)
        {
         active[activeCount] = pivots[nextPivot];
         activeCount++;
         nextPivot++;
         needRebuild = true;
        }
      if(needRebuild)
        {
         zoneCount   = BuildZones(active, activeCount, i, scanStart, high, low, zones);
         needRebuild = false;
        }
      if(zoneCount == 0)
         continue;

      // nearest support / resistance as of this bar
      double sup = EMPTY_VALUE, res = EMPTY_VALUE;
      for(int z = 0; z < zoneCount; z++)
        {
         if(zones[z].top < close[i] && (sup == EMPTY_VALUE || zones[z].top > sup))
            sup = zones[z].top;
         if(zones[z].bottom > close[i] && (res == EMPTY_VALUE || zones[z].bottom < res))
            res = zones[z].bottom;
        }
      BufSupport[i] = sup;
      BufResist[i]  = res;

      for(int z = 0; z < zoneCount; z++)
        {
         bool up   = false;
         bool down = false;

         if(InpSignalMode == SR_MODE_RETEST)
           {
            up   = IsBreakRetest(i, zones[z], 1, open, high, low, close, tick_volume);
            down = IsBreakRetest(i, zones[z], -1, open, high, low, close, tick_volume);
           }
         else
           {
            up   = IsConfirmedBreak(i, zones[z], 1, open, high, low, close, tick_volume);
            down = IsConfirmedBreak(i, zones[z], -1, open, high, low, close, tick_volume);
           }

         if(up)
           {
            BufBreakUp[i] = low[i] - 0.6 * g_atr[i];
            BufSignal[i]  = SIG_BREAK_UP;
            break;
           }
         if(down)
           {
            BufBreakDown[i] = high[i] + 0.6 * g_atr[i];
            BufSignal[i]    = SIG_BREAK_DOWN;
            break;
           }
         if(IsFalseBreak(i, zones[z], 1, high, low, close))
           {
            BufFakeUp[i] = high[i] + 0.6 * g_atr[i];
            BufSignal[i] = SIG_FAKE_UP;
            break;
           }
         if(IsFalseBreak(i, zones[z], -1, high, low, close))
           {
            BufFakeDown[i] = low[i] - 0.6 * g_atr[i];
            BufSignal[i]   = SIG_FAKE_DOWN;
            break;
           }
        }
     }

   // refresh the zone set with the latest ATR for drawing and for the levels
   zoneCount = BuildZones(active, activeCount, lastClosed, scanStart, high, low, zones);

   double lastCloseValue = close[lastClosed];
   double sup = EMPTY_VALUE, res = EMPTY_VALUE;
   for(int z = 0; z < zoneCount; z++)
     {
      if(zones[z].top < lastCloseValue && (sup == EMPTY_VALUE || zones[z].top > sup))
         sup = zones[z].top;
      if(zones[z].bottom > lastCloseValue && (res == EMPTY_VALUE || zones[z].bottom < res))
         res = zones[z].bottom;
     }
   BufSupport[lastClosed]      = sup;
   BufResist[lastClosed]       = res;
   BufSupport[rates_total - 1] = sup;      // so an EA reading bar 0 gets the live level
   BufResist[rates_total - 1]  = res;

   DrawZones(zones, zoneCount, time, rates_total, lastCloseValue);

   if(!firstPass && BufSignal[lastClosed] != 0.0 && time[lastClosed] != g_lastAlertTime)
     {
      g_lastAlertTime = time[lastClosed];
      RaiseAlert(BufSignal[lastClosed], time[lastClosed], close[lastClosed]);
     }

   return rates_total;
  }
//+------------------------------------------------------------------+

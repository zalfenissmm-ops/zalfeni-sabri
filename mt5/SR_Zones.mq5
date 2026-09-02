//+------------------------------------------------------------------+
//|                                                   SR_Zones.mq5   |
//|                 Support & Resistance zones — nothing else         |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property link      "https://github.com/zalfenissmm-ops/zalfeni-sabri"
#property version   "1.00"
#property description "Draws support/resistance ZONES by clustering swing pivots and"
#property description "scoring each one: reaction vs noise, distinct touches, recency,"
#property description "role reversal, and proximity to a round number. No signals."

#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2

#property indicator_label1  "Nearest Support"
#property indicator_type1   DRAW_NONE

#property indicator_label2  "Nearest Resistance"
#property indicator_type2   DRAW_NONE

input group             "=== Detection ==="
input int    InpLookback       = 600;    // Bars to analyse
input int    InpSwing          = 3;      // Swing (pivot) window
input int    InpATRPeriod      = 14;     // ATR period
input double InpZoneWidthATR   = 0.5;    // Zone width (x ATR)
input int    InpMinTouches     = 2;      // Min distinct touches
input double InpMinScore       = 60.0;   // Min zone score (0-100)
input int    InpMaxZones       = 6;      // Max zones drawn

input group             "=== Display ==="
input bool   InpShowLabels     = true;          // Show the label on each zone
input int    InpExtendBars     = 10;            // Extend zones this many bars right
input color  InpSupportColor   = clrSeaGreen;   // Zone below price
input color  InpResistColor    = clrFireBrick;  // Zone above price
input color  InpFlipColor      = clrGoldenrod;  // Zone that reversed roles

//--- score weights (must sum to 1.0)
#define W_REACTION  0.40
#define W_TOUCHES   0.20
#define W_RECENCY   0.20
#define W_FLIP      0.10
#define W_ROUND     0.10

#define TOUCHES_FOR_FULL_SCORE  5
#define REACTION_LOOKAHEAD      10
// Reaction is measured against what a random walk covers over the same horizon:
// 1.0 is pure noise, 2.0 is twice that. A flat ATR multiple would score noise
// full marks — over 10 bars a drift-free walk already travels ~3.2 x ATR.
#define REACTION_NOISE_FLOOR    1.0
#define REACTION_FULL_SCORE     2.0
#define FLIP_LOOKBACK           40

double BufSupport[];
double BufResist[];

double g_atr[];
const string OBJ_PREFIX = "SRZ_";

struct SRPivot
  {
   int      bar;
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
   double   reaction;   // average reaction vs noise (1.0 = random walk)
   double   score;      // 0..100
  };

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpSwing < 1 || InpATRPeriod < 2 || InpMinTouches < 1)
     {
      Print("SR_Zones: Swing and MinTouches must be >= 1, ATRPeriod >= 2.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpLookback < InpSwing * 4 + InpATRPeriod)
     {
      Print("SR_Zones: Lookback is too small for the chosen Swing/ATR period.");
      return(INIT_PARAMETERS_INCORRECT);
     }

   SetIndexBuffer(0, BufSupport, INDICATOR_DATA);
   SetIndexBuffer(1, BufResist,  INDICATOR_DATA);
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   IndicatorSetString(INDICATOR_SHORTNAME, "S/R Zones (" + IntegerToString(InpSwing) + ")");
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
//| How far price ran away from a pivot, measured against what a      |
//| random walk covers over the same horizon (ATR x sqrt(bars)).      |
//+------------------------------------------------------------------+
double ReactionVsNoise(const SRPivot &p, const int upToBar,
                       const double &high[], const double &low[])
  {
   double a = g_atr[p.bar];
   if(a <= 0.0)
      return 0.0;

   int end = p.bar + REACTION_LOOKAHEAD;
   if(end > upToBar) end = upToBar;
   if(end <= p.bar) return 0.0;
   double noise = a * MathSqrt((double)(end - p.bar));
   if(noise <= 0.0) return 0.0;

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
   return MathMax(move, 0.0) / noise;
  }

//+------------------------------------------------------------------+
//| Distance to the nearest psychological round number, 0..1.         |
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
void SortByBar(SRPivot &arr[], const int count)
  {
   for(int i = 1; i < count; i++)
     {
      SRPivot key = arr[i];
      int j = i - 1;
      while(j >= 0 && arr[j].bar > key.bar)
        {
         arr[j + 1] = arr[j];
         j--;
        }
      arr[j + 1] = key;
     }
  }

//+------------------------------------------------------------------+
//| Cluster the pivots into scored zones. Highs and lows are grouped  |
//| together on purpose: broken resistance becomes support, and both  |
//| touches belong to the same zone.                                  |
//+------------------------------------------------------------------+
int BuildZones(const SRPivot &pivots[], const int pivotCount, const int upToBar,
               const int scanStart, const double &high[], const double &low[],
               const double &close[], SRZone &zones[])
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

   SRPivot member[];
   SRPivot touch[];
   ArrayResize(member, pivotCount);
   ArrayResize(touch, pivotCount);

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
         // support/resistance is an area, never a line
         double bottom = sorted[clusterStart].price;
         double top    = sorted[i - 1].price;
         if(top - bottom < band)
           {
            double mid = (bottom + top) / 2.0;
            bottom = mid - band / 2.0;
            top    = mid + band / 2.0;
           }

         for(int k = 0; k < members; k++)
            member[k] = sorted[clusterStart + k];
         SortByBar(member, members);

         // Pivots from the same visit are ONE touch. A new touch counts only
         // once price has closed outside the zone since the previous one.
         touch[0] = member[0];
         int nTouch = 1;
         for(int k = 1; k < members; k++)
           {
            bool leftZone = false;
            for(int b = touch[nTouch - 1].bar + 1; b < member[k].bar; b++)
               if(close[b] > top || close[b] < bottom)
                 {
                  leftZone = true;
                  break;
                 }
            if(leftZone)
              {
               touch[nTouch] = member[k];
               nTouch++;
              }
           }

         if(nTouch >= InpMinTouches)
           {
            SRZone z;
            z.bottom     = bottom;
            z.top        = top;
            z.firstTouch = touch[0].bar;
            z.lastTouch  = touch[nTouch - 1].bar;
            z.touches    = nTouch;

            double sumPrice = 0.0;
            double sumReact = 0.0;
            bool   fromAbove = false;
            bool   fromBelow = false;

            for(int k = 0; k < nTouch; k++)
              {
               sumPrice += touch[k].price;
               sumReact += ReactionVsNoise(touch[k], upToBar, high, low);

               int stop = touch[k].bar - FLIP_LOOKBACK;
               if(stop < scanStart)
                  stop = scanStart;
               for(int b = touch[k].bar - 1; b >= stop; b--)
                 {
                  if(close[b] > top)
                    {
                     fromAbove = true;
                     break;
                    }
                  if(close[b] < bottom)
                    {
                     fromBelow = true;
                     break;
                    }
                 }
              }

            // a real role reversal also needs price to have traded a full ATR
            // clear of the zone on both sides, not merely closed past its edge
            bool heldAbove = false;
            bool heldBelow = false;
            for(int b = z.firstTouch; b <= z.lastTouch; b++)
              {
               if(close[b] > top + g_atr[b])    heldAbove = true;
               if(close[b] < bottom - g_atr[b]) heldBelow = true;
              }

            z.center   = sumPrice / nTouch;
            z.reaction = sumReact / nTouch;
            z.flipped  = (fromAbove && fromBelow && heldAbove && heldBelow);

            int    counted     = (nTouch < TOUCHES_FOR_FULL_SCORE) ? nTouch : TOUCHES_FOR_FULL_SCORE;
            double touchPart   = (double)counted / (double)TOUCHES_FOR_FULL_SCORE;
            double excess      = (z.reaction - REACTION_NOISE_FLOOR) /
                                 (REACTION_FULL_SCORE - REACTION_NOISE_FLOOR);
            double reactPart   = MathMax(0.0, MathMin(excess, 1.0));
            double span        = (double)(upToBar - scanStart);
            double recencyPart = (span > 0.0) ? (double)(z.lastTouch - scanStart) / span : 0.0;
            double flipPart    = z.flipped ? 1.0 : 0.0;
            double roundPart   = RoundNumberScore(z.center);

            z.score = 100.0 * (W_REACTION * reactPart +
                               W_TOUCHES  * touchPart +
                               W_RECENCY  * recencyPart +
                               W_FLIP     * flipPart +
                               W_ROUND    * roundPart);

            if(z.score >= InpMinScore)
              {
               zones[zoneCount] = z;
               zoneCount++;
              }
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
void DrawZones(const SRZone &zones[], const int zoneCount,
               const datetime &time[], const int rates_total, const double lastClose)
  {
   ObjectsDeleteAll(0, OBJ_PREFIX);

   datetime rightEdge = time[rates_total - 1] + (datetime)(PeriodSeconds() * InpExtendBars);

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

      if(!InpShowLabels)
         continue;

      string label = OBJ_PREFIX + "T" + IntegerToString(z);
      ObjectCreate(0, label, OBJ_TEXT, 0, rightEdge, zones[z].center);
      ObjectSetString(0, label, OBJPROP_TEXT,
                      StringFormat("%s %.0f | %d touches | react %.1fx", role, zones[z].score,
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
   ArraySetAsSeries(high, false);
   ArraySetAsSeries(low, false);
   ArraySetAsSeries(close, false);
   ArraySetAsSeries(BufSupport, false);
   ArraySetAsSeries(BufResist, false);

   // zones only change when a new bar closes
   static datetime lastBarTime = 0;
   if(prev_calculated > 0 && time[rates_total - 1] == lastBarTime)
      return rates_total;
   lastBarTime = time[rates_total - 1];

   for(int i = 0; i < rates_total; i++)
     {
      BufSupport[i] = EMPTY_VALUE;
      BufResist[i]  = EMPTY_VALUE;
     }

   if(rates_total < InpSwing * 4 + InpATRPeriod + 10)
      return 0;

   ComputeATR(high, low, close, rates_total, InpATRPeriod);

   int scanStart = rates_total - InpLookback;
   if(scanStart < 0)
      scanStart = 0;
   int lastClosed = rates_total - 2;
   if(lastClosed <= scanStart + InpSwing * 2)
      return rates_total;

   SRPivot pivots[];
   int pivotCount = FindPivots(high, low, scanStart, lastClosed, InpSwing, pivots);
   if(pivotCount == 0)
     {
      ObjectsDeleteAll(0, OBJ_PREFIX);
      ChartRedraw();
      return rates_total;
     }

   SRZone zones[];
   int zoneCount = BuildZones(pivots, pivotCount, lastClosed, scanStart, high, low, close, zones);

   double lastClose = close[lastClosed];
   double sup = EMPTY_VALUE;
   double res = EMPTY_VALUE;
   for(int z = 0; z < zoneCount; z++)
     {
      if(zones[z].top < lastClose && (sup == EMPTY_VALUE || zones[z].top > sup))
         sup = zones[z].top;
      if(zones[z].bottom > lastClose && (res == EMPTY_VALUE || zones[z].bottom < res))
         res = zones[z].bottom;
     }
   BufSupport[lastClosed]      = sup;
   BufResist[lastClosed]       = res;
   BufSupport[rates_total - 1] = sup;
   BufResist[rates_total - 1]  = res;

   DrawZones(zones, zoneCount, time, rates_total, lastClose);
   return rates_total;
  }
//+------------------------------------------------------------------+

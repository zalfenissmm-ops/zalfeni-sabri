//+------------------------------------------------------------------+
//|                                           SupportResistance.mq5  |
//|                                                                  |
//|  Support/resistance ZONES, scored 0-100 and ranked, drawn on the |
//|  chart. A level is never a single line: nearby swing points are  |
//|  clustered into one zone whose width is measured in ATR, so the  |
//|  indicator adapts itself to any symbol and any timeframe.        |
//|                                                                  |
//|  Score = touches + reaction size + recency + role reversal,      |
//|          minus a penalty for decisive breaks.                    |
//|                                                                  |
//|  No repaint: a swing point is only used from the bar it could be |
//|  confirmed on (index + SwingWindow), and only closed bars are    |
//|  analysed, so a zone never appears earlier than it really did.   |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property version   "1.00"
#property description "Support & resistance zones, scored 0-100 and ranked"
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

input int    InpLookback     = 600;            // Bars to analyse
input int    InpSwing        = 3;              // Swing window (bars each side)
input int    InpAtrPeriod    = 14;             // ATR period
input double InpMerge        = 0.35;           // Zone merge distance (x ATR)
input double InpMaxWidthATR  = 0.7;            // Hard cap on zone width (x ATR)
input int    InpMaxWidthPts  = 0;              // Hard cap on zone width (points, 0 = off)
input int    InpMinTouches   = 1;              // Minimum touches to keep a zone
input int    InpMinGap       = 5;              // Min bars between two touches
input int    InpReactWindow  = 10;             // Bars used to measure a reaction
input double InpHalfLife     = 50.0;           // Recency half-life (bars)
input int    InpTopN         = 3;              // Zones drawn on each side
input double InpMinScore     = 0.0;            // Hide zones scoring below this
input bool   InpHideBroken   = false;          // Hide zones marked "broken"
input color  InpSupportColor = clrSeaGreen;    // Support colour
input color  InpResistColor  = clrIndianRed;   // Resistance colour
input bool   InpShowLabels   = true;           // Show score labels
input int    InpExtendBars   = 12;             // Extend zones this far to the right
input bool   InpAlertOnTouch = false;          // Alert when price enters a zone

#define PREFIX       "SRZ_"

// Score weights — the whole "is this level worth trading" judgement is here.
#define W_TOUCH      35.0
#define W_REACTION   30.0
#define W_RECENCY    20.0
#define FLIP_BONUS   15.0
#define ROUND_BONUS  5.0
#define BREAK_PEN    25.0

#define MAX_TOUCHES  5.0    // touches beyond this add nothing
#define STRONG_REACT 2.0    // a 2 ATR bounce already earns full credit
#define FLIP_REACT   0.5    // reaction needed after a break to call it a flip
#define BREAK_BUFFER 0.25   // a close must clear the zone by this much ATR

enum ZoneStatus
  {
   ZONE_FRESH,      // formed and never revisited
   ZONE_TESTED,     // revisited and held
   ZONE_BROKEN,     // price went through and stayed through
   ZONE_FLIPPED     // broken, then held from the other side
  };

struct Pivot
  {
   int      index;        // bar the pivot sits on
   int      confirmed;    // first bar it could be known on
   int      kind;         // +1 swing high, -1 swing low
   double   top;          // wick-to-body band
   double   bottom;
  };

struct Zone
  {
   double     top;
   double     bottom;
   int        born;
   int        highs;        // pivots of each kind that formed it: the dominant
   int        lows;         // side is the edge that must survive a width clamp
   int        touches;      // revisits; the formation counts on top of these
   double     react_sum;
   int        react_n;
   int        last_touch;
   int        breaks;
   int        first_break;
   int        last_break;
   ZoneStatus status;
   double     score;
  };

MqlRates g_rates[];
double   g_atr[];
Zone     g_zones[];
datetime g_last_alert = 0;

// Inputs are read-only, so the validated values live here.
int    g_lookback;
int    g_swing;
int    g_atr_period;
double g_merge;
double g_max_width_atr;
double g_max_width_pts;
int    g_min_touches;
int    g_min_gap;
int    g_react_window;
double g_half_life;
int    g_top_n;
int    g_extend;

//+------------------------------------------------------------------+
int OnInit()
  {
   // Clamp everything a user can type into a range that still computes.
   g_swing        = (int)MathMax(1, InpSwing);
   g_atr_period   = (int)MathMax(2, InpAtrPeriod);
   g_merge        = MathMax(0.01, InpMerge);
   g_max_width_atr = MathMax(0.05, InpMaxWidthATR);
   g_max_width_pts = MathMax(0.0, (double)InpMaxWidthPts);
   g_min_touches  = (int)MathMax(1, InpMinTouches);
   g_min_gap      = (int)MathMax(1, InpMinGap);
   g_react_window = (int)MathMax(1, InpReactWindow);
   g_half_life    = MathMax(1.0, InpHalfLife);
   g_top_n        = (int)MathMax(1, InpTopN);
   g_extend       = (int)MathMax(0, InpExtendBars);
   g_lookback     = (int)MathMax(g_swing * 2 + g_atr_period + 10, InpLookback);

   IndicatorSetString(INDICATOR_SHORTNAME, "S/R Zones");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, PREFIX);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| ATR is the measuring stick everywhere; never let it reach zero.  |
//+------------------------------------------------------------------+
double Unit(const int i)
  {
   if(i >= 0 && i < ArraySize(g_atr) && g_atr[i] > 0.0)
      return(g_atr[i]);
   return(_Point > 0 ? _Point : 1e-9);
  }

//+------------------------------------------------------------------+
//| Wilder ATR, aligned bar-for-bar with g_rates. Bars before the    |
//| period is filled use the running mean of what is available, so a |
//| short history still produces usable numbers.                     |
//+------------------------------------------------------------------+
void BuildAtr(const int bars)
  {
   ArrayResize(g_atr, bars);
   double running = 0.0;

   for(int i = 0; i < bars; i++)
     {
      double tr;
      if(i == 0)
         tr = g_rates[0].high - g_rates[0].low;
      else
        {
         double a = g_rates[i].high - g_rates[i].low;
         double b = MathAbs(g_rates[i].high - g_rates[i - 1].close);
         double c = MathAbs(g_rates[i].low  - g_rates[i - 1].close);
         tr = MathMax(a, MathMax(b, c));
        }

      if(i < g_atr_period)
        {
         running += tr;
         g_atr[i] = running / (i + 1);
        }
      else
         g_atr[i] = (g_atr[i - 1] * (g_atr_period - 1) + tr) / g_atr_period;
     }
  }

//+------------------------------------------------------------------+
//| Fractal swing highs/lows. Each contributes a wick-to-body band:  |
//| the wick shows where liquidity was taken, the body shows where   |
//| price was actually accepted. That band is the zone.              |
//+------------------------------------------------------------------+
int BuildPivots(const int bars, Pivot &out[])
  {
   int n = 0;
   ArrayResize(out, 0);

   for(int i = g_swing; i < bars - g_swing; i++)
     {
      bool is_high = true, is_low = true;
      for(int k = i - g_swing; k <= i + g_swing; k++)
        {
         if(g_rates[k].high > g_rates[i].high) is_high = false;
         if(g_rates[k].low  < g_rates[i].low)  is_low  = false;
        }

      if(is_high)
        {
         ArrayResize(out, n + 1);
         out[n].index     = i;
         out[n].confirmed = i + g_swing;
         out[n].kind      = 1;
         out[n].top       = g_rates[i].high;
         out[n].bottom    = MathMax(g_rates[i].open, g_rates[i].close);
         n++;
        }
      if(is_low)
        {
         ArrayResize(out, n + 1);
         out[n].index     = i;
         out[n].confirmed = i + g_swing;
         out[n].kind      = -1;
         out[n].top       = MathMin(g_rates[i].open, g_rates[i].close);
         out[n].bottom    = g_rates[i].low;
         n++;
        }
     }
   return(n);
  }

//+------------------------------------------------------------------+
//| The widest a zone is allowed to be. A zone thicker than this is  |
//| not a level any more, it is a region, and price can sit inside   |
//| it without telling you anything.                                 |
//+------------------------------------------------------------------+
double MaxWidth(const double unit)
  {
   double limit = g_max_width_atr * unit;
   if(g_max_width_pts > 0.0)
      limit = MathMin(limit, g_max_width_pts * _Point);
   return(limit);
  }

//+------------------------------------------------------------------+
//| Store a finished cluster as a zone.                              |
//|                                                                  |
//| Two corrections happen here. A pivot with no wick would give a    |
//| zero-width zone, so every zone gets a minimum thickness. And a    |
//| cluster of tall candles can span far more than a level should, so |
//| the zone is clamped to MaxWidth() — anchored on the side its      |
//| pivots agree on, because that extreme IS the level: for a cluster |
//| of swing highs the high must survive and the body edge gives way. |
//+------------------------------------------------------------------+
int PushZone(double bottom, double top, const int born, const double gap,
             const double unit, const int highs, const int lows, const int n)
  {
   double limit = MaxWidth(unit);

   double minimum = MathMin(0.2 * gap, limit);
   double missing = minimum - (top - bottom);
   if(missing > 0)
     {
      top    += missing / 2.0;
      bottom -= missing / 2.0;
     }

   double excess = (top - bottom) - limit;
   if(excess > 0)
     {
      if(highs > lows)
         bottom = top - limit;            // keep the swing high
      else
         if(lows > highs)
            top = bottom + limit;         // keep the swing low
         else
           {
            top    -= excess / 2.0;       // no dominant side: shrink evenly
            bottom += excess / 2.0;
           }
     }

   ArrayResize(g_zones, n + 1);
   g_zones[n].top         = top;
   g_zones[n].bottom      = bottom;
   g_zones[n].born        = born;
   g_zones[n].highs       = highs;
   g_zones[n].lows        = lows;
   g_zones[n].touches     = 0;
   g_zones[n].react_sum   = 0.0;
   g_zones[n].react_n     = 0;
   g_zones[n].last_touch  = born;
   g_zones[n].breaks      = 0;
   g_zones[n].first_break = -1;
   g_zones[n].last_break  = -1;
   g_zones[n].status      = ZONE_FRESH;
   g_zones[n].score       = 0.0;
   return(n + 1);
  }

//+------------------------------------------------------------------+
//| Group pivots whose bands sit within g_merge ATR of each other.   |
//| Highs and lows are clustered together on purpose: one price area |
//| is one level, and a zone holding both kinds is exactly what a    |
//| role reversal looks like. A zone is capped at twice the merge    |
//| distance so a chain of pivots cannot grow to swallow the chart.  |
//+------------------------------------------------------------------+
int BuildZones(Pivot &pivots[], const int count)
  {
   ArrayResize(g_zones, 0);
   if(count <= 0)
      return(0);

   // Order pivots by the centre of their band (selection sort: counts are small).
   int order[];
   ArrayResize(order, count);
   for(int i = 0; i < count; i++)
      order[i] = i;

   for(int i = 0; i < count - 1; i++)
     {
      int best = i;
      for(int j = i + 1; j < count; j++)
        {
         double cj = (pivots[order[j]].top + pivots[order[j]].bottom) / 2.0;
         double cb = (pivots[order[best]].top + pivots[order[best]].bottom) / 2.0;
         if(cj < cb)
            best = j;
        }
      int tmp = order[i];
      order[i] = order[best];
      order[best] = tmp;
     }

   int    n = 0;
   bool   open_zone = false;
   double cur_top = 0.0, cur_bottom = 0.0, last_gap = 0.0, last_unit = 0.0;
   int    cur_born = 0, cur_highs = 0, cur_lows = 0;

   for(int i = 0; i < count; i++)
     {
      Pivot  p    = pivots[order[i]];
      double unit = Unit(p.index);
      double gap  = g_merge * unit;

      // A pivot joins the open zone only if it is close enough AND the zone
      // stays inside the width cap: merging must never widen a level past the
      // point where it stops being one.
      bool joins = open_zone
                   && (p.bottom - cur_top) <= gap
                   && (MathMax(cur_top, p.top) - MathMin(cur_bottom, p.bottom)) <= MaxWidth(unit);

      if(joins)
        {
         cur_top    = MathMax(cur_top, p.top);
         cur_bottom = MathMin(cur_bottom, p.bottom);
         cur_born   = MathMin(cur_born, p.confirmed);
         if(p.kind > 0)
            cur_highs++;
         else
            cur_lows++;
        }
      else
        {
         if(open_zone)
            n = PushZone(cur_bottom, cur_top, cur_born, last_gap, last_unit, cur_highs, cur_lows, n);
         cur_top    = p.top;
         cur_bottom = p.bottom;
         cur_born   = p.confirmed;
         cur_highs  = (p.kind > 0) ? 1 : 0;
         cur_lows   = (p.kind > 0) ? 0 : 1;
         open_zone  = true;
        }

      last_gap  = gap;
      last_unit = unit;
     }

   if(open_zone)
      n = PushZone(cur_bottom, cur_top, cur_born, last_gap, last_unit, cur_highs, cur_lows, n);
   return(n);
  }

//+------------------------------------------------------------------+
//| How far price travelled in the direction the zone was supposed   |
//| to send it, measured in ATR. A level that produced a 3 ATR bounce|
//| is worth far more than one price merely crawled across.          |
//+------------------------------------------------------------------+
double MeasureReaction(const int z, const int i, const int side, const double unit, const int bars)
  {
   int last = MathMin(i + g_react_window, bars - 1);
   if(last <= i)
      return(0.0);

   double hi = g_rates[i + 1].high;
   double lo = g_rates[i + 1].low;
   for(int k = i + 2; k <= last; k++)
     {
      hi = MathMax(hi, g_rates[k].high);
      lo = MathMin(lo, g_rates[k].low);
     }

   double up   = (hi - g_zones[z].top) / unit;
   double down = (g_zones[z].bottom - lo) / unit;

   double reaction;
   if(side > 0)            // came from above: the zone must hold as support
      reaction = up;
   else
      if(side < 0)         // came from below: the zone must hold as resistance
         reaction = down;
      else
         reaction = MathMax(up, down);

   return(MathMax(0.0, reaction));
  }

//+------------------------------------------------------------------+
//| Walk forward from the bar the zone became known, recording each  |
//| distinct touch and each decisive break.                          |
//|                                                                  |
//| Which side price sits on is a state, not a bar-to-bar compare: a |
//| close inside the zone or its buffer leaves the state alone. That |
//| way drifting along an edge never fakes a break, and a genuine    |
//| crossing is never missed because one bar closed on the boundary. |
//+------------------------------------------------------------------+
void ScanZone(const int z, const int bars)
  {
   int  last_touch = -1;
   int  side = 0;               // 0 unknown, +1 above the zone, -1 below it
   bool flipped = false;

   for(int i = g_zones[z].born; i < bars; i++)
     {
      double unit = Unit(i);

      if(g_rates[i].high >= g_zones[z].bottom && g_rates[i].low <= g_zones[z].top)
        {
         // One consolidation must not be counted as five separate touches.
         if(last_touch < 0 || (i - last_touch) >= g_min_gap)
           {
            double reaction = MeasureReaction(z, i, side, unit, bars);
            g_zones[z].touches++;
            g_zones[z].react_sum += reaction;
            g_zones[z].react_n++;
            g_zones[z].last_touch = i;
            last_touch = i;

            if(g_zones[z].first_break >= 0 && i > g_zones[z].first_break && reaction >= FLIP_REACT)
               flipped = true;
           }
        }

      int new_side = side;
      if(g_rates[i].close > g_zones[z].top + BREAK_BUFFER * unit)
         new_side = 1;
      else
         if(g_rates[i].close < g_zones[z].bottom - BREAK_BUFFER * unit)
            new_side = -1;

      if(side != 0 && new_side != side)
        {
         g_zones[z].breaks++;
         if(g_zones[z].first_break < 0)
            g_zones[z].first_break = i;
         g_zones[z].last_break = i;
        }
      side = new_side;
     }

   if(flipped)
      g_zones[z].status = ZONE_FLIPPED;
   else
      if(g_zones[z].breaks > 0 && (g_zones[z].touches == 0 || g_zones[z].last_break > g_zones[z].last_touch))
         g_zones[z].status = ZONE_BROKEN;
      else
         if(g_zones[z].touches > 0)
            g_zones[z].status = ZONE_TESTED;
         else
            g_zones[z].status = ZONE_FRESH;
  }

//+------------------------------------------------------------------+
//| Psychological levels. The round-number step follows the price    |
//| magnitude, so it works for 1.0850, 2340.0 and 61000 alike.       |
//+------------------------------------------------------------------+
bool BracketsRoundNumber(const int z)
  {
   double price = (g_zones[z].top + g_zones[z].bottom) / 2.0;
   if(price <= 0.0)
      return(false);

   double step = MathPow(10.0, MathFloor(MathLog10(price))) / 100.0;
   if(step <= 0.0)
      return(false);

   return(MathFloor(g_zones[z].bottom / step) != MathFloor(g_zones[z].top / step));
  }

//+------------------------------------------------------------------+
void ScoreZone(const int z, const int last_index)
  {
   double touch_count = g_zones[z].touches + 1;   // formation counts as touch 1
   double score = MathMin(touch_count, MAX_TOUCHES) / MAX_TOUCHES * W_TOUCH;

   if(g_zones[z].react_n > 0)
     {
      double avg = g_zones[z].react_sum / g_zones[z].react_n;
      score += MathMin(avg / STRONG_REACT, 1.0) * W_REACTION;
     }

   double age = (double)(last_index - g_zones[z].last_touch);
   score += MathPow(0.5, age / g_half_life) * W_RECENCY;

   if(g_zones[z].status == ZONE_FLIPPED)
      score += FLIP_BONUS;
   else
      if(g_zones[z].status == ZONE_BROKEN)
         score -= BREAK_PEN;

   if(BracketsRoundNumber(z))
      score += ROUND_BONUS;

   g_zones[z].score = MathMax(0.0, MathMin(100.0, score));
  }

//+------------------------------------------------------------------+
string StatusText(const ZoneStatus s)
  {
   switch(s)
     {
      case ZONE_FLIPPED: return("flipped");
      case ZONE_BROKEN:  return("broken");
      case ZONE_TESTED:  return("tested");
     }
   return("fresh");
  }

//+------------------------------------------------------------------+
void DrawZone(const int z, const string tag, const bool is_support, const int bars)
  {
   string   name = PREFIX + tag;
   datetime t1   = g_rates[MathMax(0, MathMin(g_zones[z].born, bars - 1))].time;
   datetime t2   = g_rates[bars - 1].time + (datetime)(PeriodSeconds() * g_extend);
   color    clr  = is_support ? InpSupportColor : InpResistColor;

   ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, g_zones[z].bottom, t2, g_zones[z].top);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FILL, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   // A broken zone is dotted so it reads as history, not as a live level.
   ObjectSetInteger(0, name, OBJPROP_STYLE,
                    g_zones[z].status == ZONE_BROKEN ? STYLE_DOT : STYLE_SOLID);

   if(!InpShowLabels)
      return;

   string label = PREFIX + "T" + tag;
   string text  = StringFormat("%.0f | %d touches | %s",
                               g_zones[z].score,
                               g_zones[z].touches + 1,
                               StatusText(g_zones[z].status));

   ObjectCreate(0, label, OBJ_TEXT, 0, t2, g_zones[z].top);
   ObjectSetString(0, label, OBJPROP_TEXT, text);
   ObjectSetInteger(0, label, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, label, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, label, OBJPROP_ANCHOR, ANCHOR_RIGHT_UPPER);
   ObjectSetInteger(0, label, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, label, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void Rebuild()
  {
   int available = Bars(_Symbol, _Period);
   int want = MathMin(g_lookback, available);
   if(want < g_swing * 2 + g_atr_period + 4)
      return;

   ArraySetAsSeries(g_rates, false);        // index 0 = oldest
   int copied = CopyRates(_Symbol, _Period, 0, want, g_rates);
   if(copied <= 1)
      return;

   // Drop the still-forming bar: its high/low keep moving, and a zone that
   // shifts inside a bar is a repainting zone.
   int bars = copied - 1;
   if(bars < g_swing * 2 + 2)
      return;

   BuildAtr(bars);

   Pivot pivots[];
   int pivot_count = BuildPivots(bars, pivots);
   int zone_count  = BuildZones(pivots, pivot_count);
   if(zone_count <= 0)
      return;

   int last_index = bars - 1;
   for(int z = 0; z < zone_count; z++)
     {
      ScanZone(z, bars);
      ScoreZone(z, last_index);
     }

   // Rank by score, then keep the best g_top_n on each side of price.
   int order[];
   ArrayResize(order, zone_count);
   for(int i = 0; i < zone_count; i++)
      order[i] = i;

   for(int i = 0; i < zone_count - 1; i++)
     {
      int best = i;
      for(int j = i + 1; j < zone_count; j++)
         if(g_zones[order[j]].score > g_zones[order[best]].score)
            best = j;
      int tmp = order[i];
      order[i] = order[best];
      order[best] = tmp;
     }

   ObjectsDeleteAll(0, PREFIX);

   // Zones are found on closed bars, but which side of them price is on is a
   // live question — use the current bid for that.
   double price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(price <= 0.0)
      price = g_rates[last_index].close;

   int  drawn_support = 0, drawn_resist = 0, drawn_inside = 0;
   bool touching = false;

   for(int i = 0; i < zone_count; i++)
     {
      int z = order[i];

      if(g_zones[z].score < InpMinScore)
         continue;
      if(g_zones[z].touches + 1 < g_min_touches)
         continue;
      if(InpHideBroken && g_zones[z].status == ZONE_BROKEN)
         continue;

      if(g_zones[z].top < price)                // below price: support
        {
         if(drawn_support < g_top_n)
           {
            DrawZone(z, "S" + IntegerToString(drawn_support), true, bars);
            drawn_support++;
           }
        }
      else
         if(g_zones[z].bottom > price)           // above price: resistance
           {
            if(drawn_resist < g_top_n)
              {
               DrawZone(z, "R" + IntegerToString(drawn_resist), false, bars);
               drawn_resist++;
              }
           }
         else                                    // price is sitting inside it
           {
            if(drawn_inside < g_top_n)
              {
               bool as_support = price > (g_zones[z].top + g_zones[z].bottom) / 2.0;
               DrawZone(z, "I" + IntegerToString(drawn_inside), as_support, bars);
               drawn_inside++;
               touching = true;
              }
           }
     }

   if(InpAlertOnTouch && touching && g_rates[last_index].time != g_last_alert)
     {
      g_last_alert = g_rates[last_index].time;
      Alert(StringFormat("%s %s: price entered an S/R zone at %s",
                         _Symbol,
                         EnumToString((ENUM_TIMEFRAMES)_Period),
                         DoubleToString(price, _Digits)));
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
   // Zones only change when a bar closes, so recompute once per bar.
   static datetime last_bar = 0;
   datetime current = (datetime)SeriesInfoInteger(_Symbol, _Period, SERIES_LASTBAR_DATE);

   if(prev_calculated == 0 || current != last_bar)
     {
      last_bar = current;
      Rebuild();
     }

   return(rates_total);
  }
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//|                                               LiquidityZones.mq5 |
//|              Liquidity zones for MetaTrader 5 (chart indicator)  |
//+------------------------------------------------------------------+
//| Marks where stop-loss and pending orders rest, and whether that   |
//| liquidity is still there:                                        |
//|   1. Fractal swing highs/lows are CLUSTERED into zones with an    |
//|      ATR-scaled tolerance, so "equal highs/lows" adapt to the     |
//|      instrument's own volatility instead of a fixed pip value.    |
//|      The zone level is the cluster extreme (stops sit BEYOND it), |
//|      and the drawn pocket extends past that extreme.              |
//|   2. Each zone is scored 0-100 by confluence: touches, cluster    |
//|      tightness, recency, volume at the touches, round-number      |
//|      proximity, and agreement with a reference level             |
//|      (prev day/week high-low, session high-low).                  |
//|   3. Each zone carries a tap status: untapped (live target),      |
//|      swept (wick took it, closed back), broken (closed beyond).   |
//|      Consumed liquidity is discounted so targets stay on what is  |
//|      still resting.                                               |
//|                                                                  |
//| Buffer 0 = nearest untapped level above price (for iCustom/EAs)   |
//| Buffer 1 = nearest untapped level below price                     |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property link      "https://github.com/zalfenissmm-ops/zalfeni-sabri"
#property version   "1.00"
#property description "Liquidity zones: clustered equal highs/lows + reference levels, scored by confluence and tagged untapped / swept / broken."
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2

#define PREFIX      "LQZ_"
#define ZONE_UNTAPPED 0
#define ZONE_SWEPT    1
#define ZONE_BROKEN   2

//--- detection -----------------------------------------------------
input group             "Detection"
input int               InpSwingWindow   = 3;      // Swing window (bars each side)
input double            InpToleranceATR  = 0.25;   // Cluster tolerance (x ATR)
input int               InpATRPeriod     = 14;     // ATR period
input int               InpLookbackBars  = 500;    // Bars to scan
input int               InpMinTouches    = 1;      // Minimum touches per zone
input int               InpMaxZones      = 12;     // Max zones drawn (best score first)
input double            InpMinScore      = 0.0;    // Minimum score to draw (0-100)
//--- reference levels ----------------------------------------------
input group             "Reference levels"
input bool              InpUsePrevDay    = true;   // Previous day high/low (PDH/PDL)
input bool              InpUsePrevWeek   = true;   // Previous week high/low (PWH/PWL)
input bool              InpUseSessions   = true;   // Session high/low (server time)
input int               InpAsiaStart     = 0;      // Asia session start hour
input int               InpAsiaEnd       = 8;      // Asia session end hour
input int               InpLondonStart   = 8;      // London session start hour
input int               InpLondonEnd     = 13;     // London session end hour
input int               InpNewYorkStart  = 13;     // New York session start hour
input int               InpNewYorkEnd    = 22;     // New York session end hour
//--- display -------------------------------------------------------
input group             "Display"
input bool              InpShowSwept     = true;   // Show swept zones
input bool              InpShowBroken    = false;  // Show broken zones
input color             InpBuySideColor  = clrTomato;      // Liquidity above price (buy stops)
input color             InpSellSideColor = clrDodgerBlue;  // Liquidity below price (sell stops)
input color             InpConsumedColor = clrDimGray;     // Swept / broken zones
input bool              InpFillZones     = true;   // Fill zone rectangles
input bool              InpShowLabels    = true;   // Show zone labels
input int               InpLabelFontSize = 8;      // Label font size
input int               InpExtendBars    = 10;     // Extend zones to the right (bars)
//--- alerts --------------------------------------------------------
input group             "Alerts"
input bool              InpAlertOnSweep  = true;   // Alert when a zone gets swept
input bool              InpAlertPopup    = true;   // Popup alert
input bool              InpAlertPush     = false;  // Push notification

//--- types ---------------------------------------------------------
struct SwingPoint
  {
   int               bar;
   double            price;
  };

struct RefLevel
  {
   string            name;
   double            price;
   bool              isHigh;
  };

struct LiquidityZone
  {
   bool              isHigh;
   double            level;      // cluster extreme — stops rest beyond it
   double            top;        // drawn pocket
   double            bottom;
   int               firstBar;
   int               lastBar;
   int               touches;
   int               status;
   int               statusBar;
   double            score;
   string            tags;
  };

//--- globals -------------------------------------------------------
double         BufAbove[];
double         BufBelow[];
int            g_atrHandle  = INVALID_HANDLE;
LiquidityZone  g_zones[];
int            g_zoneCount  = 0;
double         g_atr        = 0.0;
double         g_tolerance  = 0.0;
datetime       g_lastBar    = 0;
datetime       g_lastAlert  = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0, BufAbove, INDICATOR_DATA);
   SetIndexBuffer(1, BufBelow, INDICATOR_DATA);
   ArraySetAsSeries(BufAbove, false);
   ArraySetAsSeries(BufBelow, false);
   PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_NONE);
   PlotIndexSetInteger(1, PLOT_DRAW_TYPE, DRAW_NONE);
   PlotIndexSetString(0, PLOT_LABEL, "Untapped above");
   PlotIndexSetString(1, PLOT_LABEL, "Untapped below");
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   g_atrHandle = iATR(_Symbol, _Period, InpATRPeriod);
   if(g_atrHandle == INVALID_HANDLE)
     {
      Print("LiquidityZones: cannot create ATR handle");
      return(INIT_FAILED);
     }

   IndicatorSetString(INDICATOR_SHORTNAME,
                      StringFormat("Liquidity Zones (%d, %.2fxATR)", InpSwingWindow, InpToleranceATR));
   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_atrHandle != INVALID_HANDLE)
      IndicatorRelease(g_atrHandle);
   ObjectsDeleteAll(0, PREFIX);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Session helpers                                                  |
//+------------------------------------------------------------------+
bool InSession(const int hour, const int start, const int end)
  {
   if(start <= end)
      return(hour >= start && hour < end);
   return(hour >= start || hour < end);   // session wraps midnight
  }

int SessionOf(const int hour)
  {
   if(InSession(hour, InpAsiaStart,    InpAsiaEnd))    return(0);
   if(InSession(hour, InpLondonStart,  InpLondonEnd))  return(1);
   if(InSession(hour, InpNewYorkStart, InpNewYorkEnd)) return(2);
   return(-1);
  }

string SessionName(const int session)
  {
   if(session == 0) return("Asia");
   if(session == 1) return("London");
   return("NY");
  }

//+------------------------------------------------------------------+
//| Psychological round numbers, scaled to the instrument's price     |
//+------------------------------------------------------------------+
double RoundNumberBonus(const double level, string &tag)
  {
   tag = "";
   if(level <= 0.0)
      return(0.0);
   double step = MathPow(10, MathFloor(MathLog10(level) - 3));
   double major = step * 10.0;
   double minor = step * 5.0;
   if(MathAbs(level - MathRound(level / major) * major) <= major * 0.05)
     {
      tag = "00";
      return(8.0);
     }
   if(MathAbs(level - MathRound(level / minor) * minor) <= minor * 0.05)
     {
      tag = "50";
      return(4.0);
     }
   return(0.0);
  }

//+------------------------------------------------------------------+
void AddRef(RefLevel &refs[], int &count, const string name, const double price, const bool isHigh)
  {
   if(price <= 0.0)
      return;
   ArrayResize(refs, count + 1);
   refs[count].name   = name;
   refs[count].price  = price;
   refs[count].isHigh = isHigh;
   count++;
  }

//+------------------------------------------------------------------+
//| Previous day / week levels and the last completed session ranges  |
//+------------------------------------------------------------------+
void CollectReferenceLevels(const datetime &time[], const double &high[], const double &low[],
                            const int rates_total, const int from, RefLevel &refs[], int &count)
  {
   count = 0;
   ArrayFree(refs);

   if(InpUsePrevDay)
     {
      AddRef(refs, count, "PDH", iHigh(_Symbol, PERIOD_D1, 1), true);
      AddRef(refs, count, "PDL", iLow(_Symbol, PERIOD_D1, 1), false);
     }
   if(InpUsePrevWeek)
     {
      AddRef(refs, count, "PWH", iHigh(_Symbol, PERIOD_W1, 1), true);
      AddRef(refs, count, "PWL", iLow(_Symbol, PERIOD_W1, 1), false);
     }
   if(!InpUseSessions || _Period >= PERIOD_D1)
      return;

   //--- split the scanned range into contiguous (day, session) blocks
   int    blockStart[];
   int    blockEnd[];
   int    blockSession[];
   int    blocks = 0;
   string lastKey = "";
   for(int i = from; i < rates_total; i++)
     {
      MqlDateTime dt;
      TimeToStruct(time[i], dt);
      int session = SessionOf(dt.hour);
      if(session < 0)
        {
         lastKey = "";
         continue;
        }
      string key = StringFormat("%04d%02d%02d-%d", dt.year, dt.mon, dt.day, session);
      if(key != lastKey)
        {
         blocks++;
         ArrayResize(blockStart, blocks);
         ArrayResize(blockEnd, blocks);
         ArrayResize(blockSession, blocks);
         blockStart[blocks - 1]   = i;
         blockSession[blocks - 1] = session;
         lastKey = key;
        }
      blockEnd[blocks - 1] = i;
     }

   //--- newest block may still be open, so start one back
   bool done[3] = {false, false, false};
   for(int b = blocks - 2; b >= 0; b--)
     {
      int session = blockSession[b];
      if(done[session])
         continue;
      done[session] = true;
      double h = high[blockStart[b]];
      double l = low[blockStart[b]];
      for(int k = blockStart[b] + 1; k <= blockEnd[b]; k++)
        {
         h = MathMax(h, high[k]);
         l = MathMin(l, low[k]);
        }
      AddRef(refs, count, SessionName(session) + " High", h, true);
      AddRef(refs, count, SessionName(session) + " Low", l, false);
     }
  }

//+------------------------------------------------------------------+
//| Fractal swings, sorted by price (ready for clustering)            |
//+------------------------------------------------------------------+
int CollectSwings(const double &price[], const int rates_total, const int from,
                  const bool wantHigh, SwingPoint &points[])
  {
   int count = 0;
   ArrayFree(points);
   int w = (int)MathMax(1, InpSwingWindow);
   for(int i = (int)MathMax(from, w); i <= rates_total - 1 - w; i++)
     {
      bool extreme = true;
      for(int k = i - w; k <= i + w && extreme; k++)
        {
         if(k == i)
            continue;
         if(wantHigh  && price[k] > price[i]) extreme = false;
         if(!wantHigh && price[k] < price[i]) extreme = false;
        }
      if(!extreme)
         continue;
      ArrayResize(points, count + 1);
      points[count].bar   = i;
      points[count].price = price[i];
      count++;
     }

   //--- insertion sort by price (swing counts are small)
   for(int i = 1; i < count; i++)
     {
      SwingPoint key = points[i];
      int j = i - 1;
      while(j >= 0 && points[j].price > key.price)
        {
         points[j + 1] = points[j];
         j--;
        }
      points[j + 1] = key;
     }
   return(count);
  }

//+------------------------------------------------------------------+
//| Where has this level been taken already?                          |
//+------------------------------------------------------------------+
void ResolveStatus(const double &high[], const double &low[], const double &close[],
                   const int rates_total, LiquidityZone &zone)
  {
   double eps = _Point * 0.5;
   zone.status    = ZONE_UNTAPPED;
   zone.statusBar = -1;
   for(int i = zone.lastBar + 1; i < rates_total; i++)
     {
      if(zone.isHigh && high[i] > zone.level + eps)
        {
         if(zone.statusBar < 0)
           {
            zone.status    = ZONE_SWEPT;
            zone.statusBar = i;
           }
         if(close[i] > zone.level + eps)
           {
            zone.status = ZONE_BROKEN;
            return;
           }
        }
      if(!zone.isHigh && low[i] < zone.level - eps)
        {
         if(zone.statusBar < 0)
           {
            zone.status    = ZONE_SWEPT;
            zone.statusBar = i;
           }
         if(close[i] < zone.level - eps)
           {
            zone.status = ZONE_BROKEN;
            return;
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| Confluence score, 0-100, discounted once liquidity is consumed    |
//+------------------------------------------------------------------+
void ScoreZone(LiquidityZone &zone, const long &tick_volume[], const int rates_total, const int from,
               RefLevel &refs[], const int refCount, const double avgVolume)
  {
   string tags = (zone.touches >= 2) ? (zone.isHigh ? "EQH" : "EQL") : (zone.isHigh ? "High" : "Low");

   double touchScore = MathMin((double)zone.touches, 4.0) / 4.0 * 30.0;

   double width      = zone.isHigh ? (zone.level - zone.bottom) : (zone.top - zone.level);
   double tightness  = (g_tolerance <= 0.0) ? 12.0 : MathMax(0.0, 1.0 - width / g_tolerance) * 12.0;

   double span       = (double)MathMax(1, rates_total - 1 - from);
   double recency    = (double)(zone.lastBar - from) / span * 10.0;

   double refScore = 0.0;
   for(int r = 0; r < refCount; r++)
     {
      if(refs[r].isHigh != zone.isHigh)
         continue;
      if(MathAbs(refs[r].price - zone.level) <= g_tolerance)
        {
         refScore = 20.0;
         tags    += " " + refs[r].name;
         break;
        }
     }

   double volumeScore = 6.0;   // neutral when volume is unusable
   if(avgVolume > 0.0)
     {
      double touched = ((double)tick_volume[zone.firstBar] + (double)tick_volume[zone.lastBar]) / 2.0;
      double ratio   = touched / avgVolume;
      volumeScore    = MathMin(MathMax(ratio - 1.0, 0.0), 1.0) * 12.0;
      if(ratio >= 1.3)
         tags += " vol";
     }

   string roundTag;
   double roundScore = RoundNumberBonus(zone.level, roundTag);
   if(roundTag != "")
      tags += " " + roundTag;

   double raw     = touchScore + tightness + recency + refScore + volumeScore + roundScore;
   double penalty = 1.0;
   if(zone.status == ZONE_SWEPT)  penalty = 0.5;
   if(zone.status == ZONE_BROKEN) penalty = 0.25;

   zone.score = raw / 92.0 * 100.0 * penalty;
   zone.tags  = tags;
  }

//+------------------------------------------------------------------+
//| Cluster one side's swings into zones                              |
//+------------------------------------------------------------------+
void BuildSide(const bool isHigh, const double &price[], const double &high[], const double &low[],
               const double &close[], const long &tick_volume[], const int rates_total, const int from,
               RefLevel &refs[], const int refCount, const double avgVolume)
  {
   SwingPoint points[];
   int total = CollectSwings(price, rates_total, from, isHigh, points);
   if(total <= 0)
      return;

   double pad = g_tolerance * 0.5;
   int    i   = 0;
   while(i < total)
     {
      int j = i;
      while(j + 1 < total && points[j + 1].price - points[i].price <= g_tolerance)
         j++;

      LiquidityZone zone;
      zone.isHigh   = isHigh;
      zone.touches  = j - i + 1;
      zone.bottom   = points[i].price;
      zone.top      = points[j].price;
      zone.level    = isHigh ? points[j].price : points[i].price;
      zone.firstBar = points[i].bar;
      zone.lastBar  = points[i].bar;
      for(int k = i; k <= j; k++)
        {
         zone.firstBar = (int)MathMin(zone.firstBar, points[k].bar);
         zone.lastBar  = (int)MathMax(zone.lastBar, points[k].bar);
        }
      //--- the pocket of orders sits BEYOND the extreme, so extend that side
      if(isHigh) zone.top    = zone.level + pad;
      else       zone.bottom = zone.level - pad;

      i = j + 1;
      if(zone.touches < InpMinTouches)
         continue;

      ResolveStatus(high, low, close, rates_total, zone);
      ScoreZone(zone, tick_volume, rates_total, from, refs, refCount, avgVolume);

      ArrayResize(g_zones, g_zoneCount + 1);
      g_zones[g_zoneCount] = zone;
      g_zoneCount++;
     }
  }

//+------------------------------------------------------------------+
void SortZonesByScore()
  {
   for(int i = 1; i < g_zoneCount; i++)
     {
      LiquidityZone key = g_zones[i];
      int j = i - 1;
      while(j >= 0 && g_zones[j].score < key.score)
        {
         g_zones[j + 1] = g_zones[j];
         j--;
        }
      g_zones[j + 1] = key;
     }
  }

//+------------------------------------------------------------------+
//| Full rebuild — runs on each new bar                               |
//+------------------------------------------------------------------+
void RebuildZones(const datetime &time[], const double &high[], const double &low[],
                  const double &close[], const long &tick_volume[], const int rates_total)
  {
   g_zoneCount = 0;
   ArrayFree(g_zones);

   int from = (int)MathMax(0, rates_total - (int)MathMax(50, InpLookbackBars));

   double atrBuf[1];
   g_atr = 0.0;
   if(CopyBuffer(g_atrHandle, 0, 0, 1, atrBuf) > 0)
      g_atr = atrBuf[0];
   g_tolerance = g_atr * InpToleranceATR;
   if(g_tolerance <= 0.0)
      g_tolerance = close[rates_total - 1] * 0.001;

   RefLevel refs[];
   int refCount = 0;
   CollectReferenceLevels(time, high, low, rates_total, from, refs, refCount);

   double volumeSum = 0.0;
   int    volumeN   = 0;
   for(int i = from; i < rates_total; i++)
     {
      volumeSum += (double)tick_volume[i];
      volumeN++;
     }
   double avgVolume = (volumeN > 0) ? volumeSum / volumeN : 0.0;

   BuildSide(true,  high, high, low, close, tick_volume, rates_total, from, refs, refCount, avgVolume);
   BuildSide(false, low,  high, low, close, tick_volume, rates_total, from, refs, refCount, avgVolume);
   SortZonesByScore();
  }

//+------------------------------------------------------------------+
bool ZoneVisible(const LiquidityZone &zone)
  {
   if(zone.score < InpMinScore)
      return(false);
   if(zone.status == ZONE_SWEPT  && !InpShowSwept)
      return(false);
   if(zone.status == ZONE_BROKEN && !InpShowBroken)
      return(false);
   return(true);
  }

string StatusName(const int status)
  {
   if(status == ZONE_SWEPT)  return("swept");
   if(status == ZONE_BROKEN) return("broken");
   return("untapped");
  }

//+------------------------------------------------------------------+
void DrawZones(const datetime &time[], const int rates_total)
  {
   ObjectsDeleteAll(0, PREFIX);
   datetime rightEdge = time[rates_total - 1] + (datetime)(PeriodSeconds() * (int)MathMax(1, InpExtendBars));

   int drawn = 0;
   for(int z = 0; z < g_zoneCount && drawn < InpMaxZones; z++)
     {
      LiquidityZone zone = g_zones[z];
      if(!ZoneVisible(zone))
         continue;
      drawn++;

      color clr = zone.isHigh ? InpBuySideColor : InpSellSideColor;
      if(zone.status != ZONE_UNTAPPED)
         clr = InpConsumedColor;

      string tag  = PREFIX + IntegerToString(z);
      string rect = tag + "_box";
      if(ObjectCreate(0, rect, OBJ_RECTANGLE, 0, time[zone.firstBar], zone.top, rightEdge, zone.bottom))
        {
         ObjectSetInteger(0, rect, OBJPROP_COLOR, clr);
         ObjectSetInteger(0, rect, OBJPROP_FILL, InpFillZones && zone.status == ZONE_UNTAPPED);
         ObjectSetInteger(0, rect, OBJPROP_BACK, true);
         ObjectSetInteger(0, rect, OBJPROP_STYLE, zone.status == ZONE_UNTAPPED ? STYLE_SOLID : STYLE_DOT);
         ObjectSetInteger(0, rect, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, rect, OBJPROP_HIDDEN, true);
        }

      string line = tag + "_lvl";
      if(ObjectCreate(0, line, OBJ_TREND, 0, time[zone.firstBar], zone.level, rightEdge, zone.level))
        {
         ObjectSetInteger(0, line, OBJPROP_COLOR, clr);
         ObjectSetInteger(0, line, OBJPROP_RAY_RIGHT, false);
         ObjectSetInteger(0, line, OBJPROP_WIDTH, zone.status == ZONE_UNTAPPED ? 2 : 1);
         ObjectSetInteger(0, line, OBJPROP_STYLE, zone.status == ZONE_UNTAPPED ? STYLE_SOLID : STYLE_DOT);
         ObjectSetInteger(0, line, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, line, OBJPROP_HIDDEN, true);
        }

      if(InpShowLabels)
        {
         string text = tag + "_txt";
         if(ObjectCreate(0, text, OBJ_TEXT, 0, rightEdge, zone.level))
           {
            ObjectSetString(0, text, OBJPROP_TEXT,
                            StringFormat(" %s x%d | %.0f | %s", zone.tags, zone.touches, zone.score, StatusName(zone.status)));
            ObjectSetString(0, text, OBJPROP_FONT, "Arial");
            ObjectSetInteger(0, text, OBJPROP_FONTSIZE, InpLabelFontSize);
            ObjectSetInteger(0, text, OBJPROP_COLOR, clr);
            ObjectSetInteger(0, text, OBJPROP_ANCHOR, ANCHOR_LEFT);
            ObjectSetInteger(0, text, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, text, OBJPROP_HIDDEN, true);
           }
        }
     }
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Nearest untapped level on each side, published to the buffers     |
//+------------------------------------------------------------------+
void PublishTargets(const double lastClose, const int rates_total)
  {
   double above = EMPTY_VALUE, below = EMPTY_VALUE;
   for(int z = 0; z < g_zoneCount; z++)
     {
      if(g_zones[z].status != ZONE_UNTAPPED)
         continue;
      double level = g_zones[z].level;
      if(level > lastClose && (above == EMPTY_VALUE || level < above))
         above = level;
      if(level < lastClose && (below == EMPTY_VALUE || level > below))
         below = level;
     }
   BufAbove[rates_total - 1] = above;
   BufBelow[rates_total - 1] = below;
  }

//+------------------------------------------------------------------+
void RaiseSweepAlert(const LiquidityZone &zone, const datetime barTime)
  {
   if(!InpAlertOnSweep || barTime == g_lastAlert)
      return;
   g_lastAlert = barTime;
   string message = StringFormat("%s %s: %s liquidity swept at %s (%s, score %.0f)",
                                 _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
                                 zone.isHigh ? "Buy-side" : "Sell-side",
                                 DoubleToString(zone.level, _Digits), zone.tags, zone.score);
   if(InpAlertPopup)
      Alert(message);
   if(InpAlertPush)
      SendNotification(message);
   Print(message);
  }

//+------------------------------------------------------------------+
//| Intrabar: has the forming bar just taken an untapped zone?        |
//+------------------------------------------------------------------+
bool CheckLiveSweep(const double &high[], const double &low[], const datetime &time[], const int rates_total)
  {
   double eps     = _Point * 0.5;
   int    last    = rates_total - 1;
   bool   changed = false;
   for(int z = 0; z < g_zoneCount; z++)
     {
      if(g_zones[z].status != ZONE_UNTAPPED || g_zones[z].lastBar >= last)
         continue;
      bool taken = g_zones[z].isHigh ? (high[last] > g_zones[z].level + eps)
                                     : (low[last]  < g_zones[z].level - eps);
      if(!taken)
         continue;
      g_zones[z].status    = ZONE_SWEPT;
      g_zones[z].statusBar = last;
      g_zones[z].score    *= 0.5;
      changed = true;
      RaiseSweepAlert(g_zones[z], time[last]);
     }
   return(changed);
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
   if(rates_total < InpSwingWindow * 2 + InpATRPeriod + 5)
      return(0);

   ArraySetAsSeries(time, false);
   ArraySetAsSeries(high, false);
   ArraySetAsSeries(low, false);
   ArraySetAsSeries(close, false);
   ArraySetAsSeries(tick_volume, false);

   //--- clear once; afterwards every bar keeps the value it had while current
   if(prev_calculated == 0)
     {
      ArrayInitialize(BufAbove, EMPTY_VALUE);
      ArrayInitialize(BufBelow, EMPTY_VALUE);
     }

   bool newBar = (time[rates_total - 1] != g_lastBar);
   if(newBar || prev_calculated == 0)
     {
      g_lastBar = time[rates_total - 1];
      RebuildZones(time, high, low, close, tick_volume, rates_total);
      DrawZones(time, rates_total);
     }
   else if(CheckLiveSweep(high, low, time, rates_total))
     {
      SortZonesByScore();
      DrawZones(time, rates_total);
     }

   PublishTargets(close[rates_total - 1], rates_total);
   return(rates_total);
  }
//+------------------------------------------------------------------+

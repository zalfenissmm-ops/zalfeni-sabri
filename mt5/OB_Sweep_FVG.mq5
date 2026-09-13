//+------------------------------------------------------------------+
//|                                                 OB_Sweep_FVG.mq5 |
//|            Order Block = Liquidity Sweep + Shift + Imbalance     |
//|            أوردر بلوك = كنس سيولة + شفت + فجوة سعرية              |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property version   "1.10"
#property description "Valid Order Block = Liquidity Sweep -> Shift -> Imbalance (FVG)"
#property description "الأوردر بلوك الصحيح = كنس سيولة + شفت + فجوة سعرية"
#property description "Clean zones, no overlap, all timeframes / مناطق نظيفة بلا تداخل على كل الفريمات"
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2
//--- plot 1 : bullish order block marker
#property indicator_label1  "Bullish OB"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrMediumSeaGreen
#property indicator_width1  1
//--- plot 2 : bearish order block marker
#property indicator_label2  "Bearish OB"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrIndianRed
#property indicator_width2  1

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group             "=== Detection / الكشف ==="
input int    InpSweepLookback   = 10;    // Sweep lookback bars (شموع البحث عن السيولة)
input bool   InpRequireOppColor = true;  // OB candle opposite colour (لون الشمعة معاكس للاتجاه)
input bool   InpRequireShift    = true;  // Shift must close beyond OB candle (اشتراط الشفت)
input bool   InpRequireFVG      = true;  // FVG required to be Valid (الفجوة شرط للصلاحية)
input double InpMinFVGPoints    = 0;     // Min FVG size in points (أدنى حجم للفجوة بالنقاط)
input double InpMinZonePoints   = 0;     // Min OB height in points (أدنى ارتفاع للمنطقة)
input bool   InpWickToWick      = true;  // OB zone = full range (المنطقة بالذيول لا بالجسم)
input int    InpMaxBars         = 3000;  // History bars to scan (عدد الشموع المفحوصة)

input group             "=== Clean chart / تنظيف الشارت ==="
input bool   InpNoOverlap       = true;  // Drop older overlapping zone (منع تداخل المناطق)
input int    InpOverlapPct      = 50;    // Overlap % that counts as duplicate (نسبة التداخل)
input bool   InpHideBroken      = true;  // Remove zone once price breaks it (حذف المنطقة المكسورة)
input bool   InpShowInvalid     = false; // Show invalid order blocks (إظهار الأوردر بلوك الخاطئ)
input bool   InpShowFVG         = true;  // Show imbalance / FVG box (إظهار الفجوة)
input bool   InpShowSweepLine   = false; // Show swept liquidity level (خط السيولة المكنوسة)
input bool   InpShowArrows      = false; // Arrow on the OB candle (سهم على الشمعة)
input int    InpMaxZones        = 12;    // Max zones kept on chart (أقصى عدد مناطق)
input int    InpMaxLabels       = 4;     // Label only the newest N zones (ليبل لآخر N مناطق)
input int    InpExtendBars      = 6;     // Extend live zones N bars right (مدّ المناطق يمينًا)

input group             "=== Style / الشكل ==="
input bool   InpFillZones       = true;  // Filled zones, else outline only (تعبئة أو إطار فقط)
input int    InpFontSize        = 7;     // Label font size (حجم الخط)
input color  InpBullOBColor     = C'126,188,158'; // Bullish OB (أخضر فاتح)
input color  InpBearOBColor     = C'214,150,150'; // Bearish OB (أحمر فاتح)
input color  InpFVGColor        = C'160,172,200'; // Imbalance / FVG (أزرق فاتح)
input color  InpInvalidColor    = C'186,186,186'; // Invalid OB (رمادي فاتح)
input color  InpSweepColor      = C'160,160,160'; // Liquidity sweep level
input color  InpTextColor       = C'145,145,145'; // Labels

input group             "=== Alerts / التنبيهات ==="
input bool   InpAlertNew        = false; // Alert on new valid OB
input bool   InpAlertRetest     = false; // Alert when price returns into a valid OB
input bool   InpPushAlerts      = false; // Send push notification too

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
#define ZONE_FRESH   0
#define ZONE_TOUCHED 1
#define ZONE_BROKEN  2

struct SZone
  {
   datetime          tStart;      // time of the order block candle (candle 1)
   int               dir;         // +1 bullish, -1 bearish
   bool              valid;       // sweep + shift + FVG
   bool              hasFVG;
   double            obTop;
   double            obBottom;
   double            fvgTop;
   double            fvgBottom;
   double            sweepLevel;
   datetime          tSweepFrom;  // bar that created the swept level
   int               state;       // ZONE_FRESH / ZONE_TOUCHED / ZONE_BROKEN
   int               checked;     // next bar index still to be checked
   datetime          tBroken;
   bool              retestAlerted;
  };

double  BullBuf[];
double  BearBuf[];
SZone   g_zones[];
string  g_pfx      = "OBSF_";
int     g_nextBar  = 0;
int     g_lookback = 10;

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
string TfName()
  {
   return StringSubstr(EnumToString((ENUM_TIMEFRAMES)Period()),7);
  }

string ZoneBase(const SZone &z)
  {
   return g_pfx+"Z"+(string)(long)z.tStart+(z.dir>0 ? "B" : "S")+"_";
  }

void ObjRect(const string name,datetime t1,double p1,datetime t2,double p2,color clr,bool fill)
  {
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_RECTANGLE,0,t1,p1,t2,p2);
      ObjectSetInteger(0,name,OBJPROP_BACK,true);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_SELECTED,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
      ObjectSetInteger(0,name,OBJPROP_WIDTH,1);
     }
   ObjectSetInteger(0,name,OBJPROP_FILL,fill);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectMove(0,name,0,t1,p1);
   ObjectMove(0,name,1,t2,p2);
  }

void ObjSegment(const string name,datetime t1,double p1,datetime t2,double p2,color clr)
  {
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_TREND,0,t1,p1,t2,p2);
      ObjectSetInteger(0,name,OBJPROP_RAY_LEFT,false);
      ObjectSetInteger(0,name,OBJPROP_RAY_RIGHT,false);
      ObjectSetInteger(0,name,OBJPROP_STYLE,STYLE_DOT);
      ObjectSetInteger(0,name,OBJPROP_WIDTH,1);
      ObjectSetInteger(0,name,OBJPROP_BACK,true);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
     }
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectMove(0,name,0,t1,p1);
   ObjectMove(0,name,1,t2,p2);
  }

void ObjLabelText(const string name,datetime t,double p,const string txt,color clr)
  {
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_TEXT,0,t,p);
      ObjectSetInteger(0,name,OBJPROP_ANCHOR,ANCHOR_LEFT);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
      ObjectSetString(0,name,OBJPROP_FONT,"Arial");
     }
   ObjectSetInteger(0,name,OBJPROP_FONTSIZE,MathMax(6,InpFontSize));
   ObjectSetString(0,name,OBJPROP_TEXT,txt);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectMove(0,name,0,t,p);
  }

void Notify(const string msg)
  {
   Alert(msg);
   if(InpPushAlerts)
      SendNotification(msg);
  }

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0,BullBuf,INDICATOR_DATA);
   SetIndexBuffer(1,BearBuf,INDICATOR_DATA);
   ArraySetAsSeries(BullBuf,false);
   ArraySetAsSeries(BearBuf,false);

   PlotIndexSetInteger(0,PLOT_ARROW,233);
   PlotIndexSetInteger(1,PLOT_ARROW,234);
   PlotIndexSetInteger(0,PLOT_ARROW_SHIFT,12);
   PlotIndexSetInteger(1,PLOT_ARROW_SHIFT,-12);
   PlotIndexSetDouble(0,PLOT_EMPTY_VALUE,EMPTY_VALUE);
   PlotIndexSetDouble(1,PLOT_EMPTY_VALUE,EMPTY_VALUE);
   PlotIndexSetString(0,PLOT_LABEL,"Bullish OB");
   PlotIndexSetString(1,PLOT_LABEL,"Bearish OB");
//--- buffers stay filled for iCustom() even when the arrows are hidden
   if(!InpShowArrows)
     {
      PlotIndexSetInteger(0,PLOT_DRAW_TYPE,DRAW_NONE);
      PlotIndexSetInteger(1,PLOT_DRAW_TYPE,DRAW_NONE);
     }

   g_lookback = MathMax(2,InpSweepLookback);
   g_pfx      = "OBSF_"+(string)ChartID()+"_";
   ObjectsDeleteAll(0,g_pfx);
   ArrayResize(g_zones,0);
   g_nextBar  = 0;

   IndicatorSetString(INDICATOR_SHORTNAME,"OB Sweep+Shift+FVG ("+(string)g_lookback+")");
   IndicatorSetInteger(INDICATOR_DIGITS,_Digits);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0,g_pfx);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Detect a setup where bar s is the order block candle             |
//+------------------------------------------------------------------+
bool Detect(const int s,
            const datetime &time[],
            const double &open[],
            const double &high[],
            const double &low[],
            const double &close[],
            SZone &z)
  {
//--- swept liquidity levels of the previous g_lookback bars
   double prevLow  = low[s-1];
   double prevHigh = high[s-1];
   int    idxLow   = s-1;
   int    idxHigh  = s-1;
   for(int k=s-g_lookback; k<=s-1; k++)
     {
      if(low[k]<prevLow)   { prevLow  = low[k];  idxLow  = k; }
      if(high[k]>prevHigh) { prevHigh = high[k]; idxHigh = k; }
     }

   double minGap  = InpMinFVGPoints*_Point;
   double minSize = InpMinZonePoints*_Point;

//--- 1) BULLISH : red candle sweeps a previous low, closes back above it
   bool bSweep = (low[s]<prevLow && close[s]>prevLow);
   bool bColor = (!InpRequireOppColor || close[s]<open[s]);
   bool bShift = InpRequireShift ? (close[s+1]>high[s] && close[s+1]>open[s+1])
                                 : (close[s+1]>open[s+1]);
   if(bSweep && bColor && bShift)
     {
      double gap    = low[s+2]-high[s];          // candle 3 low vs candle 1 high
      bool   hasFVG = (gap>0.0 && gap>=minGap);
      bool   keep   = (hasFVG || !InpRequireFVG || InpShowInvalid);
      double top    = InpWickToWick ? high[s] : MathMax(open[s],close[s]);
      double bottom = InpWickToWick ? low[s]  : MathMin(open[s],close[s]);
      if(keep && (top-bottom)>=minSize)
        {
         z.tStart     = time[s];
         z.dir        = 1;
         z.hasFVG     = hasFVG;
         z.valid      = (InpRequireFVG ? hasFVG : true);
         z.obTop      = top;
         z.obBottom   = bottom;
         z.fvgBottom  = high[s];
         z.fvgTop     = low[s+2];
         z.sweepLevel = prevLow;
         z.tSweepFrom = time[idxLow];
         z.state      = ZONE_FRESH;
         z.checked    = s+3;
         z.tBroken    = 0;
         z.retestAlerted = false;
         return(true);
        }
     }

//--- 2) BEARISH : green candle sweeps a previous high, closes back below it
   bool sSweep = (high[s]>prevHigh && close[s]<prevHigh);
   bool sColor = (!InpRequireOppColor || close[s]>open[s]);
   bool sShift = InpRequireShift ? (close[s+1]<low[s] && close[s+1]<open[s+1])
                                 : (close[s+1]<open[s+1]);
   if(sSweep && sColor && sShift)
     {
      double gap    = low[s]-high[s+2];          // candle 1 low vs candle 3 high
      bool   hasFVG = (gap>0.0 && gap>=minGap);
      bool   keep   = (hasFVG || !InpRequireFVG || InpShowInvalid);
      double top    = InpWickToWick ? high[s] : MathMax(open[s],close[s]);
      double bottom = InpWickToWick ? low[s]  : MathMin(open[s],close[s]);
      if(keep && (top-bottom)>=minSize)
        {
         z.tStart     = time[s];
         z.dir        = -1;
         z.hasFVG     = hasFVG;
         z.valid      = (InpRequireFVG ? hasFVG : true);
         z.obTop      = top;
         z.obBottom   = bottom;
         z.fvgTop     = low[s];
         z.fvgBottom  = high[s+2];
         z.sweepLevel = prevHigh;
         z.tSweepFrom = time[idxHigh];
         z.state      = ZONE_FRESH;
         z.checked    = s+3;
         z.tBroken    = 0;
         z.retestAlerted = false;
         return(true);
        }
     }

   return(false);
  }

//+------------------------------------------------------------------+
//| Zone list management                                             |
//+------------------------------------------------------------------+
void RemoveZoneAt(const int i)
  {
   int cnt = ArraySize(g_zones);
   if(i<0 || i>=cnt)
      return;
   ObjectsDeleteAll(0,ZoneBase(g_zones[i]));
   for(int k=i; k<cnt-1; k++)
      g_zones[k] = g_zones[k+1];
   ArrayResize(g_zones,cnt-1);
  }

//--- how much (%) two zones of the same side share
double OverlapPercent(const SZone &a,const SZone &b)
  {
   double hi = MathMin(a.obTop,b.obTop);
   double lo = MathMax(a.obBottom,b.obBottom);
   double inter = hi-lo;
   if(inter<=0.0)
      return(0.0);
   double smaller = MathMin(a.obTop-a.obBottom,b.obTop-b.obBottom);
   if(smaller<=0.0)
      return(0.0);
   return(inter/smaller*100.0);
  }

void AddZone(const SZone &z)
  {
//--- a fresh zone replaces an older one sitting on the same price area
   if(InpNoOverlap)
      for(int i=ArraySize(g_zones)-1; i>=0; i--)
        {
         if(g_zones[i].dir!=z.dir || g_zones[i].state==ZONE_BROKEN)
            continue;
         if(OverlapPercent(g_zones[i],z)>=(double)InpOverlapPct)
            RemoveZoneAt(i);
        }

   int n = ArraySize(g_zones);
   ArrayResize(g_zones,n+1);
   g_zones[n] = z;

//--- keep only the newest InpMaxZones zones
   while(ArraySize(g_zones)>MathMax(1,InpMaxZones))
      RemoveZoneAt(0);
  }

//+------------------------------------------------------------------+
//| Draw / refresh one zone                                          |
//+------------------------------------------------------------------+
void DrawZone(const int i,const datetime tRight,const bool withLabel)
  {
   SZone  z    = g_zones[i];
   string base = ZoneBase(z);

   if(!z.valid && !InpShowInvalid)
     {
      ObjectsDeleteAll(0,base);
      return;
     }

   color obc = z.valid ? (z.dir>0 ? InpBullOBColor : InpBearOBColor) : InpInvalidColor;
   if(z.state==ZONE_BROKEN)
      obc = InpInvalidColor;

   datetime tEnd = (z.state==ZONE_BROKEN && z.tBroken>0) ? z.tBroken : tRight;
   if(tEnd<=z.tStart)
      tEnd = (datetime)(z.tStart+PeriodSeconds());

//--- order block box
   ObjRect(base+"OB",z.tStart,z.obTop,tEnd,z.obBottom,obc,InpFillZones);

//--- imbalance / FVG box (outline only, so it never hides the candles)
   if(InpShowFVG && z.hasFVG && z.valid && z.state!=ZONE_BROKEN)
      ObjRect(base+"FVG",z.tStart,z.fvgTop,tEnd,z.fvgBottom,InpFVGColor,false);
   else
      ObjectDelete(0,base+"FVG");

//--- swept liquidity level
   if(InpShowSweepLine && z.state!=ZONE_BROKEN)
      ObjSegment(base+"SW",z.tSweepFrom,z.sweepLevel,(datetime)(z.tStart+PeriodSeconds()),z.sweepLevel,InpSweepColor);
   else
      ObjectDelete(0,base+"SW");

//--- one short label, only for the newest zones, placed right of the box
   if(withLabel && z.state!=ZONE_BROKEN)
     {
      string txt = z.valid ? (z.dir>0 ? " Bull OB" : " Bear OB") : " OB (no FVG)";
      if(z.state==ZONE_TOUCHED)
         txt += " •";
      ObjLabelText(base+"T",tEnd,(z.obTop+z.obBottom)/2.0,txt,InpTextColor);
     }
   else
      ObjectDelete(0,base+"T");
  }

//+------------------------------------------------------------------+
//| OnCalculate                                                      |
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
   if(rates_total<g_lookback+8)
      return(0);

   ArraySetAsSeries(time,false);
   ArraySetAsSeries(open,false);
   ArraySetAsSeries(high,false);
   ArraySetAsSeries(low,false);
   ArraySetAsSeries(close,false);

//--- full recalculation
   if(prev_calculated<=0)
     {
      ArrayInitialize(BullBuf,EMPTY_VALUE);
      ArrayInitialize(BearBuf,EMPTY_VALUE);
      ObjectsDeleteAll(0,g_pfx);
      ArrayResize(g_zones,0);
      g_nextBar = MathMax(g_lookback+1,rates_total-MathMax(50,InpMaxBars));
     }
   else
     {
      for(int i=MathMax(0,prev_calculated-1); i<rates_total; i++)
        {
         BullBuf[i] = EMPTY_VALUE;
         BearBuf[i] = EMPTY_VALUE;
        }
     }

//--- candle s needs s+1 and s+2 closed; last closed bar is rates_total-2
   int lastCandidate = rates_total-4;

   for(int s=MathMax(g_nextBar,g_lookback+1); s<=lastCandidate; s++)
     {
      SZone z;
      ZeroMemory(z);
      if(!Detect(s,time,open,high,low,close,z))
         continue;

      AddZone(z);

      if(z.valid)
        {
         if(z.dir>0)
            BullBuf[s] = low[s];
         else
            BearBuf[s] = high[s];
        }

      if(InpAlertNew && z.valid && s>=rates_total-6)
         Notify(_Symbol+" "+TfName()+" : new valid "+
                (z.dir>0 ? "BULLISH" : "BEARISH")+" order block "+
                DoubleToString(z.obBottom,_Digits)+" - "+DoubleToString(z.obTop,_Digits));
     }

   if(lastCandidate>=0)
      g_nextBar = MathMax(g_nextBar,lastCandidate+1);

//--- update state of every zone (mitigation / break) and redraw
   datetime tRight = (datetime)(time[rates_total-1]+PeriodSeconds()*MathMax(0,InpExtendBars));
   double   bid    = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   int      labels = MathMax(0,InpMaxLabels);

   for(int i=ArraySize(g_zones)-1; i>=0; i--)
     {
      //--- scan the closed bars that appeared since the last pass
      for(int b=MathMax(0,g_zones[i].checked); b<=rates_total-2; b++)
        {
         if(g_zones[i].state==ZONE_BROKEN)
            break;

         if(low[b]<=g_zones[i].obTop && high[b]>=g_zones[i].obBottom &&
            g_zones[i].state==ZONE_FRESH)
            g_zones[i].state = ZONE_TOUCHED;

         bool broken = (g_zones[i].dir>0) ? (close[b]<g_zones[i].obBottom)
                                          : (close[b]>g_zones[i].obTop);
         if(broken)
           {
            g_zones[i].state   = ZONE_BROKEN;
            g_zones[i].tBroken = time[b];
           }
        }
      g_zones[i].checked = MathMax(g_zones[i].checked,rates_total-1);

      //--- live retest alert
      if(InpAlertRetest && g_zones[i].valid && g_zones[i].state!=ZONE_BROKEN &&
         !g_zones[i].retestAlerted &&
         bid<=g_zones[i].obTop && bid>=g_zones[i].obBottom)
        {
         g_zones[i].retestAlerted = true;
         Notify(_Symbol+" "+TfName()+" : price back inside "+
                (g_zones[i].dir>0 ? "BULLISH" : "BEARISH")+" order block "+
                DoubleToString(g_zones[i].obBottom,_Digits)+" - "+
                DoubleToString(g_zones[i].obTop,_Digits));
        }

      //--- a broken zone is dropped from the chart by default
      if(g_zones[i].state==ZONE_BROKEN && InpHideBroken)
        {
         RemoveZoneAt(i);
         continue;
        }

      bool withLabel = (labels>0 && g_zones[i].state!=ZONE_BROKEN);
      if(withLabel)
         labels--;
      DrawZone(i,tRight,withLabel);
     }

   ChartRedraw();
   return(rates_total);
  }
//+------------------------------------------------------------------+

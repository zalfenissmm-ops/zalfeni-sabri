//+------------------------------------------------------------------+
//|                                                 OB_Sweep_FVG.mq5 |
//|      Order Block = swing liquidity sweep + structure shift + FVG |
//|      أوردر بلوك = كنس سيولة سوينق + كسر هيكل + فجوة سعرية         |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property version   "1.40"
#property description "Sweep of a real swing point -> displacement that shifts structure -> imbalance"
#property description "كنس قاع/قمة سوينق حقيقية ثم شفت يكسر الهيكل بقوة ثم فجوة سعرية"
#property description "Works on all timeframes / يعمل على جميع الفريمات"
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2
#property indicator_label1  "Bullish OB"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrMediumSeaGreen
#property indicator_width1  1
#property indicator_label2  "Bearish OB"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrIndianRed
#property indicator_width2  1

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group             "=== Quality filters / فلاتر الجودة ==="
input int    InpPivotStrength   = 3;     // Swing strength (قوة القمة/القاع: شموع كل جهة)
input int    InpSweepSearch     = 40;    // Bars searched for that swing (مدى البحث)
input bool   InpRequireSweep    = false; // Sweep of a swing is mandatory (اشتراط كنس السيولة)
input bool   InpRequireMSS      = true;  // Shift must break structure (الشفت يكسر الهيكل)
input int    InpMSSBars         = 5;     // Bars allowed for the shift (شموع مسموحة للشفت)
input double InpDisplaceATR     = 1.0;   // Shift leg >= x*ATR (قوة موجة الشفت)
input double InpMinFVGATR       = 0.10;  // FVG >= x*ATR (أدنى فجوة نسبة لـ ATR)
input double InpMaxZoneATR      = 2.0;   // Clamp zone height to x*ATR (أقصى ارتفاع للمنطقة)
input int    InpATRPeriod       = 14;    // ATR period
input bool   InpRequireOppColor = false; // OB candle opposite colour (لون معاكس)
input bool   InpRequireFVG      = true;  // No FVG = invalid (بدون فجوة = غير صالح)
input bool   InpWickToWick      = true;  // Zone = full range (المنطقة بالذيول)
input int    InpMaxBars         = 3000;  // History bars to scan (عدد الشموع المفحوصة)

input group             "=== Clean chart / تنظيف الشارت ==="
input bool   InpNoOverlap       = true;  // Newer zone replaces overlapping one (منع التداخل)
input int    InpOverlapPct      = 50;    // Overlap % that counts as duplicate
input bool   InpHideBroken      = true;  // Remove broken zones (حذف المكسورة)
input bool   InpShowInvalid     = false; // Show sweep+shift without FVG
input bool   InpShowFVG         = true;  // Show the imbalance box
input bool   InpShowSweepLine   = true;  // Show the swept swing level (خط السيولة)
input bool   InpShowMidLine     = false; // Show 50% of the zone (خط 50%)
input bool   InpShowArrows      = false; // Arrow on the OB candle
input int    InpMaxZones        = 10;    // Max zones on chart
input int    InpMaxLabels       = 4;     // Label only the newest N zones
input int    InpExtendBars      = 6;     // Extend live zones N bars right

input group             "=== Style / الشكل ==="
input bool   InpFillZones       = true;  // Fill the zone (تعبئة) - false = إطار فقط
input int    InpBorderWidth     = 2;     // Zone border width (سماكة الإطار)
input int    InpFontSize        = 8;     // Label font size
input color  InpBullFill        = C'176,214,196'; // Bullish fill (أخضر فاتح)
input color  InpBullBorder      = C'34,139,87';   // Bullish border
input color  InpBearFill        = C'240,190,190'; // Bearish fill (أحمر فاتح)
input color  InpBearBorder      = C'178,58,58';   // Bearish border
input color  InpFVGColor        = C'120,140,190'; // Imbalance / FVG outline
input color  InpInvalidColor    = C'170,170,170'; // Invalid OB
input color  InpSweepColor      = C'150,150,150'; // Swept level
input color  InpTextColor       = C'120,120,120'; // Labels

input group             "=== Alerts / التنبيهات ==="
input bool   InpAlertNew        = false; // Alert on new valid OB
input bool   InpAlertRetest     = false; // Alert when price returns into a zone
input bool   InpPushAlerts      = false; // Send push notification too

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
#define ZONE_FRESH   0
#define ZONE_TOUCHED 1
#define ZONE_BROKEN  2

struct SZone
  {
   datetime          tStart;
   int               dir;          // +1 bullish, -1 bearish
   bool              valid;
   bool              hasFVG;
   bool              hasSweep;
   double            obTop;
   double            obBottom;
   double            fvgTop;
   double            fvgBottom;
   double            sweepLevel;
   datetime          tSweepFrom;
   int               state;
   int               checked;
   datetime          tBroken;
   bool              retestAlerted;
  };

double  BullBuf[];
double  BearBuf[];
SZone   g_zones[];
string  g_pfx      = "OBSF_";
int     g_nextBar  = 0;
int     g_pivot    = 3;
int     g_search   = 40;
int     g_mssBars  = 3;
int     g_atrLen   = 14;

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
string TfName()
  {
   return StringSubstr(EnumToString((ENUM_TIMEFRAMES)Period()),7);
  }

string ZoneBase(const SZone &z)
  {
   return g_pfx+"Z"+(string)(long)z.tStart+(z.dir>0 ? "B" : "S")+"_";
  }

void ObjRect(const string name,datetime t1,double p1,datetime t2,double p2,
             color clr,bool fill,int width,int style,bool back)
  {
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_RECTANGLE,0,t1,p1,t2,p2);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_SELECTED,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
     }
   ObjectSetInteger(0,name,OBJPROP_FILL,fill);
   ObjectSetInteger(0,name,OBJPROP_BACK,back);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_WIDTH,width);
   ObjectSetInteger(0,name,OBJPROP_STYLE,style);
   ObjectMove(0,name,0,t1,p1);
   ObjectMove(0,name,1,t2,p2);
  }

void ObjSegment(const string name,datetime t1,double p1,datetime t2,double p2,
                color clr,int style,int width)
  {
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_TREND,0,t1,p1,t2,p2);
      ObjectSetInteger(0,name,OBJPROP_RAY_LEFT,false);
      ObjectSetInteger(0,name,OBJPROP_RAY_RIGHT,false);
      ObjectSetInteger(0,name,OBJPROP_BACK,true);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
     }
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_STYLE,style);
   ObjectSetInteger(0,name,OBJPROP_WIDTH,width);
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

//--- average true range at bar s (self contained, no handles)
double ATRAt(const int s,const double &high[],const double &low[],const double &close[])
  {
   if(s-g_atrLen<1)
      return(0.0);
   double sum=0.0;
   for(int i=s-g_atrLen+1; i<=s; i++)
     {
      double tr = MathMax(high[i]-low[i],
                  MathMax(MathAbs(high[i]-close[i-1]),MathAbs(low[i]-close[i-1])));
      sum += tr;
     }
   return(sum/g_atrLen);
  }

//--- nearest CONFIRMED swing low that is still unswept when bar s prints
int FindSwingLow(const int s,const double &low[])
  {
   int    k   = g_pivot;
   double run = DBL_MAX;
   int    from = MathMax(k,s-g_search);
   for(int q=s-1; q>=from; q--)
     {
      if(low[q]<run)
         run = low[q];      // q is a new running minimum -> level not broken since
      else
         continue;
      if(q+k>s-1)
         continue;          // pivot not confirmed yet before bar s
      bool pivot = true;
      for(int j=q-k; j<=q+k; j++)
        {
         if(j<0 || j>=s)          { pivot=false; break; }
         if(low[j]<low[q])        { pivot=false; break; }
        }
      if(pivot)
         return(q);
     }
   return(-1);
  }

//--- nearest CONFIRMED swing high that is still untouched when bar s prints
int FindSwingHigh(const int s,const double &high[])
  {
   int    k   = g_pivot;
   double run = -DBL_MAX;
   int    from = MathMax(k,s-g_search);
   for(int q=s-1; q>=from; q--)
     {
      if(high[q]>run)
         run = high[q];
      else
         continue;
      if(q+k>s-1)
         continue;
      bool pivot = true;
      for(int j=q-k; j<=q+k; j++)
        {
         if(j<0 || j>=s)          { pivot=false; break; }
         if(high[j]>high[q])      { pivot=false; break; }
        }
      if(pivot)
         return(q);
     }
   return(-1);
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
   if(!InpShowArrows)
     {
      PlotIndexSetInteger(0,PLOT_DRAW_TYPE,DRAW_NONE);
      PlotIndexSetInteger(1,PLOT_DRAW_TYPE,DRAW_NONE);
     }

   g_pivot   = MathMax(1,InpPivotStrength);
   g_search  = MathMax(g_pivot*3,InpSweepSearch);
   g_mssBars = MathMax(1,InpMSSBars);
   g_atrLen  = MathMax(2,InpATRPeriod);

   g_pfx = "OBSF_"+(string)ChartID()+"_";
   ObjectsDeleteAll(0,g_pfx);
   ArrayResize(g_zones,0);
   g_nextBar = 0;

   IndicatorSetString(INDICATOR_SHORTNAME,"OB Sweep+MSS+FVG");
   IndicatorSetInteger(INDICATOR_DIGITS,_Digits);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0,g_pfx);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Detect a setup where bar s is the order block candle             |
//+------------------------------------------------------------------+
bool Detect(const int s,const int maxIdx,
            const datetime &time[],const double &open[],const double &high[],
            const double &low[],const double &close[],SZone &z)
  {
   double atr = ATRAt(s,high,low,close);
   if(atr<=0.0)
      return(false);

   double minFVG  = InpMinFVGATR*atr;     // size of the imbalance
   double minLeg  = InpDisplaceATR*atr;   // size of the shift leg
   double maxZone = InpMaxZoneATR*atr;    // a spike candle must not become a huge zone

   int  win = MathMin(s+g_mssBars,maxIdx);
   bool isLowest = true, isHighest = true;
   for(int q=s+1; q<=win; q++)
     {
      if(low[q]<low[s])   isLowest  = false;
      if(high[q]>high[s]) isHighest = false;
     }

//================= BULLISH =========================================
   int  p         = FindSwingLow(s,low);
   bool sweptLow  = (p>=0 && low[s]<low[p]);
   bool bullSetup = isLowest &&
                    (sweptLow || (!InpRequireSweep && close[s]<open[s])) &&
                    (!InpRequireOppColor || close[s]<open[s]);
   if(bullSetup)
     {
      //--- level the shift has to break: structure high after a sweep,
      //--- otherwise just the order block candle itself
      double structHigh = high[s];
      if(sweptLow)
         for(int q=p; q<=s; q++)
            if(high[q]>structHigh)
               structHigh = high[q];
      double target = (InpRequireMSS && sweptLow) ? structHigh : high[s];

      //--- leg that shifts the market away from the swept low
      int j = -1;
      for(int x=s+1; x<=win; x++)
         if(close[x]>target && (close[x]-low[s])>=minLeg)
           { j=x; break; }

      if(j>0)
        {
         //--- first imbalance inside the impulse leg
         bool   hasFVG = false;
         double fTop=0.0, fBot=0.0;
         for(int a=s; a+2<=MathMin(j+1,maxIdx); a++)
            if((low[a+2]-high[a])>=minFVG)
              { fBot=high[a]; fTop=low[a+2]; hasFVG=true; break; }

         if(hasFVG || !InpRequireFVG || InpShowInvalid)
           {
            double bottom = InpWickToWick ? low[s]  : MathMin(open[s],close[s]);
            double top    = InpWickToWick ? high[s] : MathMax(open[s],close[s]);
            if(maxZone>0.0 && (top-bottom)>maxZone)
               top = bottom+maxZone;

            z.tStart     = time[s];
            z.dir        = 1;
            z.hasSweep   = sweptLow;
            z.hasFVG     = hasFVG;
            z.valid      = (InpRequireFVG ? hasFVG : true);
            z.obTop      = top;
            z.obBottom   = bottom;
            z.fvgTop     = fTop;
            z.fvgBottom  = fBot;
            z.sweepLevel = sweptLow ? low[p]  : low[s];
            z.tSweepFrom = sweptLow ? time[p] : time[s];
            z.state      = ZONE_FRESH;
            z.checked    = j+1;
            z.tBroken    = 0;
            z.retestAlerted = false;
            return(true);
           }
        }
     }

//================= BEARISH =========================================
   int  ph        = FindSwingHigh(s,high);
   bool sweptHigh = (ph>=0 && high[s]>high[ph]);
   bool bearSetup = isHighest &&
                    (sweptHigh || (!InpRequireSweep && close[s]>open[s])) &&
                    (!InpRequireOppColor || close[s]>open[s]);
   if(bearSetup)
     {
      double structLow = low[s];
      if(sweptHigh)
         for(int q=ph; q<=s; q++)
            if(low[q]<structLow)
               structLow = low[q];
      double target = (InpRequireMSS && sweptHigh) ? structLow : low[s];

      int j = -1;
      for(int x=s+1; x<=win; x++)
         if(close[x]<target && (high[s]-close[x])>=minLeg)
           { j=x; break; }

      if(j>0)
        {
         bool   hasFVG = false;
         double fTop=0.0, fBot=0.0;
         for(int a=s; a+2<=MathMin(j+1,maxIdx); a++)
            if((low[a]-high[a+2])>=minFVG)
              { fTop=low[a]; fBot=high[a+2]; hasFVG=true; break; }

         if(hasFVG || !InpRequireFVG || InpShowInvalid)
           {
            double top    = InpWickToWick ? high[s] : MathMax(open[s],close[s]);
            double bottom = InpWickToWick ? low[s]  : MathMin(open[s],close[s]);
            if(maxZone>0.0 && (top-bottom)>maxZone)
               bottom = top-maxZone;

            z.tStart     = time[s];
            z.dir        = -1;
            z.hasSweep   = sweptHigh;
            z.hasFVG     = hasFVG;
            z.valid      = (InpRequireFVG ? hasFVG : true);
            z.obTop      = top;
            z.obBottom   = bottom;
            z.fvgTop     = fTop;
            z.fvgBottom  = fBot;
            z.sweepLevel = sweptHigh ? high[ph] : high[s];
            z.tSweepFrom = sweptHigh ? time[ph] : time[s];
            z.state      = ZONE_FRESH;
            z.checked    = j+1;
            z.tBroken    = 0;
            z.retestAlerted = false;
            return(true);
           }
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

double OverlapPercent(const SZone &a,const SZone &b)
  {
   double inter = MathMin(a.obTop,b.obTop)-MathMax(a.obBottom,b.obBottom);
   if(inter<=0.0)
      return(0.0);
   double smaller = MathMin(a.obTop-a.obBottom,b.obTop-b.obBottom);
   if(smaller<=0.0)
      return(0.0);
   return(inter/smaller*100.0);
  }

void AddZone(const SZone &z)
  {
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

   color fillC   = z.valid ? (z.dir>0 ? InpBullFill   : InpBearFill)   : InpInvalidColor;
   color borderC = z.valid ? (z.dir>0 ? InpBullBorder : InpBearBorder) : InpInvalidColor;
   if(z.state==ZONE_BROKEN)
     {
      fillC   = InpInvalidColor;
      borderC = InpInvalidColor;
     }

   datetime tEnd = (z.state==ZONE_BROKEN && z.tBroken>0) ? z.tBroken : tRight;
   if(tEnd<=z.tStart)
      tEnd = (datetime)(z.tStart+PeriodSeconds());

//--- soft body behind the candles + crisp border in front
   if(InpFillZones)
      ObjRect(base+"OB",z.tStart,z.obTop,tEnd,z.obBottom,fillC,true,1,STYLE_SOLID,true);
   else
      ObjectDelete(0,base+"OB");
   ObjRect(base+"BD",z.tStart,z.obTop,tEnd,z.obBottom,borderC,false,
           MathMax(1,InpBorderWidth),STYLE_SOLID,false);

//--- imbalance / FVG : dotted outline only
   if(InpShowFVG && z.hasFVG && z.valid && z.state!=ZONE_BROKEN)
      ObjRect(base+"FVG",z.tStart,z.fvgTop,tEnd,z.fvgBottom,InpFVGColor,false,1,STYLE_DOT,true);
   else
      ObjectDelete(0,base+"FVG");

//--- swept swing level
   if(InpShowSweepLine && z.hasSweep && z.state!=ZONE_BROKEN)
      ObjSegment(base+"SW",z.tSweepFrom,z.sweepLevel,(datetime)(z.tStart+PeriodSeconds()),
                 z.sweepLevel,InpSweepColor,STYLE_DOT,1);
   else
      ObjectDelete(0,base+"SW");

//--- 50% of the zone
   if(InpShowMidLine && z.state!=ZONE_BROKEN)
     {
      double mid = (z.obTop+z.obBottom)/2.0;
      ObjSegment(base+"MID",z.tStart,mid,tEnd,mid,borderC,STYLE_DOT,1);
     }
   else
      ObjectDelete(0,base+"MID");

//--- one short label, to the right of the box
   if(withLabel && z.state!=ZONE_BROKEN)
     {
      string txt = z.valid ? (z.dir>0 ? " Bull OB" : " Bear OB") : " OB (no FVG)";
      if(z.hasSweep)
         txt += " +sweep";
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
   int warmup = g_search+g_atrLen+g_mssBars+8;
   if(rates_total<warmup)
      return(0);

   ArraySetAsSeries(time,false);
   ArraySetAsSeries(open,false);
   ArraySetAsSeries(high,false);
   ArraySetAsSeries(low,false);
   ArraySetAsSeries(close,false);

   if(prev_calculated<=0)
     {
      ArrayInitialize(BullBuf,EMPTY_VALUE);
      ArrayInitialize(BearBuf,EMPTY_VALUE);
      ObjectsDeleteAll(0,g_pfx);
      ArrayResize(g_zones,0);
      g_nextBar = MathMax(warmup,rates_total-MathMax(100,InpMaxBars));
     }
   else
     {
      for(int i=MathMax(0,prev_calculated-1); i<rates_total; i++)
        {
         BullBuf[i] = EMPTY_VALUE;
         BearBuf[i] = EMPTY_VALUE;
        }
     }

   int maxIdx        = rates_total-2;               // last closed bar
   int lastCandidate = maxIdx-(g_mssBars+2);        // full confirmation window available

   for(int s=MathMax(g_nextBar,warmup); s<=lastCandidate; s++)
     {
      SZone z;
      ZeroMemory(z);
      if(!Detect(s,maxIdx,time,open,high,low,close,z))
         continue;

      AddZone(z);

      if(z.valid)
        {
         if(z.dir>0)
            BullBuf[s] = low[s];
         else
            BearBuf[s] = high[s];
        }

      if(InpAlertNew && z.valid && s>=lastCandidate-1)
         Notify(_Symbol+" "+TfName()+" : new valid "+
                (z.dir>0 ? "BULLISH" : "BEARISH")+" order block "+
                DoubleToString(z.obBottom,_Digits)+" - "+DoubleToString(z.obTop,_Digits));
     }

   if(lastCandidate>=0)
      g_nextBar = MathMax(g_nextBar,lastCandidate+1);

   datetime tRight = (datetime)(time[rates_total-1]+PeriodSeconds()*MathMax(0,InpExtendBars));
   double   bid    = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   int      labels = MathMax(0,InpMaxLabels);

   for(int i=ArraySize(g_zones)-1; i>=0; i--)
     {
      for(int b=MathMax(0,g_zones[i].checked); b<=maxIdx; b++)
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
      g_zones[i].checked = MathMax(g_zones[i].checked,maxIdx+1);

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

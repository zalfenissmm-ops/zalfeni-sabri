//+------------------------------------------------------------------+
//|                                           SMC_VideoStrategy.mq5  |
//|  FVG + Liquidity sweep + BOS  --  the exact model from the video |
//|                                                                  |
//|  1) Fair Value Gap  (فجوة سعرية)                                  |
//|  2) Liquidity sweep (سيولة)                                       |
//|  3) Break of Structure as the target (كسر هيكلي)                  |
//|                                                                  |
//|  Keeps the chart as clean as the video: one trade, the gap that   |
//|  produced it, and the two levels -- nothing else. The chart's own |
//|  colors are never touched.                                        |
//|  Works on every symbol and every timeframe (nothing hardcoded).   |
//+------------------------------------------------------------------+
#property copyright   "zalfeni-sabri"
#property version     "1.20"
#property description "SMC: Fair Value Gap + Liquidity sweep + Break of Structure target."
#property description "Draws the FVG, the LQ/BOS levels and the trade box, exactly like the video."
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

#define PREFIX  "SMCV_"

//--- Setup detection ------------------------------------------------
input int    InpLookbackBars   = 600;    // Bars analysed
input int    InpSwingWindow    = 3;      // Swing (fractal) window
input int    InpReclaimBars    = 3;      // Bars allowed to reclaim the swept level
input int    InpAtrPeriod      = 14;     // ATR period (used to size the gaps)
input double InpMinFvgAtrPct   = 30.0;   // Minimum FVG height (% of ATR) - kills micro gaps
input bool   InpRequireCE      = false;  // Require the wick to reach the FVG mid (CE)
input bool   InpAllowBuys      = true;   // Detect buy setups
input bool   InpAllowSells     = true;   // Detect sell setups
input double InpMinRR          = 0.0;    // Minimum reward:risk (0 = no filter)
//--- Trade levels ---------------------------------------------------
input double InpBufferPercent  = 25.0;   // SL/TP buffer (% of FVG height)
input double InpTP1Percent     = 50.0;   // TP #1 (% of the distance to the target)
//--- Look -----------------------------------------------------------
input bool   InpAutoContrast   = true;   // Adapt the palette to a light chart background
input int    InpMaxSetups      = 1;      // Trades drawn (1 = only the latest, like the video)
input int    InpTradeBoxBars   = 30;     // Trade box width in bars
input bool   InpShowFvgBoxes   = true;   // Draw the nearest valid FVG zones
input int    InpMaxFvgBoxes    = 2;      // How many of them
input int    InpFvgMaxAgeBars  = 300;    // Ignore gaps older than this
input bool   InpShowLevels     = true;   // Draw the current LQ / BOS levels
input bool   InpShowPanel      = false;  // Checklist panel (top-left)
input double InpPipSize        = 0.0;    // Pip size for the result label (0 = auto)
//--- Alerts ---------------------------------------------------------
input bool   InpAlertPopup     = true;   // Popup alert on a new setup
input bool   InpAlertPush      = false;  // Push notification on a new setup
//--- Colors used on a dark chart (a light chart auto-adapts) --------
input color  InpFvgFill        = C'38,38,38';   // FVG box
input color  InpFvgText        = clrLimeGreen;  // "FVG" text
input color  InpLevelColor     = clrWhite;      // LQ / BOS lines
input color  InpLabelColor     = clrRoyalBlue;  // LQ / BOS / TP#1 labels
input color  InpProfitFill     = C'0,58,44';    // Profit box
input color  InpRiskFill       = C'66,18,29';   // Risk box
input color  InpTpColor        = clrKhaki;      // TP lines
input color  InpSlColor        = clrCrimson;    // SL line
input color  InpEntryColor     = clrSilver;     // Entry line

//+------------------------------------------------------------------+
//| Data structures                                                   |
//+------------------------------------------------------------------+
struct SwingPoint
  {
   int      idx;        // bar index
   double   price;      // high (swing high) or low (swing low)
   bool     isHigh;
  };

struct FvgZone
  {
   int      formed;     // index of the 3rd candle that leaves the gap
   double   top;
   double   bottom;
   bool     bullish;
   int      mitigated;  // first bar closing through the zone (= rates_total if still valid)
  };

struct TradeSetup
  {
   bool     bullish;
   int      sweepIdx;   // bar that swept the liquidity and tapped the FVG
   int      entryIdx;   // bar that reclaimed the level (trade is drawn from here)
   int      lqIdx;      // swing that provided the liquidity
   int      bosIdx;     // swing that provides the structure target
   int      fvgIdx;     // index inside g_fvg
   double   lq, bos, entry, sl, tp1, tp, fvgTop, fvgBottom;
   int      result;     // 1 target hit, -1 stop hit, 0 still running
   int      resultIdx;
   datetime entryTime;
  };

SwingPoint  g_swings[];
FvgZone     g_fvg[];
TradeSetup  g_setups[];
double      g_atr[];

double      g_curLq   = 0.0;   // last confirmed swing low  (liquidity for buys)
double      g_curBos  = 0.0;   // last confirmed swing high (target for buys)
int         g_curLqIdx  = -1;
int         g_curBosIdx = -1;
int         g_validFvg  = 0;
datetime    g_lastBarTime   = 0;
datetime    g_lastAlertTime = 0;

//--- resolved palette (dark chart -> the inputs, light chart -> readable variants)
color c_fvgFill,c_fvgText,c_fvgMid,c_level,c_label,c_profit,c_risk,c_tp,c_sl,c_entry,c_win,c_loss,c_open;

//+------------------------------------------------------------------+
//| Palette (the indicator never touches the chart colors)            |
//+------------------------------------------------------------------+
bool DarkBackground()
  {
   long bg=ChartGetInteger(0,CHART_COLOR_BACKGROUND);
   int r=(int)(bg&0xFF);
   int g=(int)((bg>>8)&0xFF);
   int b=(int)((bg>>16)&0xFF);
   return((0.299*r+0.587*g+0.114*b)<128.0);
  }

void ResolvePalette()
  {
   if(InpAutoContrast && !DarkBackground())
     {
      //--- light chart: pale fills, dark text - same layout, readable
      c_fvgFill = C'222,222,222';
      c_fvgText = C'0,110,60';
      c_fvgMid  = C'150,150,150';
      c_level   = C'60,60,60';
      c_label   = C'20,60,160';
      c_profit  = C'198,232,219';
      c_risk    = C'248,214,218';
      c_tp      = C'150,120,0';
      c_sl      = clrCrimson;
      c_entry   = C'90,90,90';
      c_win     = C'0,130,70';
      c_loss    = C'190,40,40';
      c_open    = C'80,80,80';
      return;
     }
   c_fvgFill = InpFvgFill;
   c_fvgText = InpFvgText;
   c_fvgMid  = C'150,150,150';
   c_level   = InpLevelColor;
   c_label   = InpLabelColor;
   c_profit  = InpProfitFill;
   c_risk    = InpRiskFill;
   c_tp      = InpTpColor;
   c_sl      = InpSlColor;
   c_entry   = InpEntryColor;
   c_win     = clrLime;
   c_loss    = clrTomato;
   c_open    = clrSilver;
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   IndicatorSetString(INDICATOR_SHORTNAME,"SMC Video Strategy");
   ObjectsDeleteAll(0,PREFIX);
   ResolvePalette();
//--- symbol / timeframe may have changed: force a full recalculation
   g_lastBarTime=0;
   g_lastAlertTime=0;
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0,PREFIX);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Helpers                                                           |
//+------------------------------------------------------------------+
double PipSize()
  {
   if(InpPipSize>0.0)
      return(InpPipSize);
   int d=(int)_Digits;
   if(d==2 || d==3 || d==5)     // gold-style and 5-digit FX
      return(10.0*_Point);
   return(_Point);
  }

string PipsText(const double diff)
  {
   double ps=PipSize();
   if(ps<=0.0)
      return("0");
   return(DoubleToString(diff/ps,0));
  }

string Px(const double price)
  {
   return(DoubleToString(price,_Digits));
  }

void StyleObject(const string name)
  {
   ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
   ObjectSetInteger(0,name,OBJPROP_SELECTED,false);
   ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
   ObjectSetInteger(0,name,OBJPROP_ZORDER,0);
  }

void PutBox(const string name,const datetime t1,const double p1,
            const datetime t2,const double p2,const color clr)
  {
   if(ObjectFind(0,name)<0)
      ObjectCreate(0,name,OBJ_RECTANGLE,0,t1,p1,t2,p2);
   else
     {
      ObjectMove(0,name,0,t1,p1);
      ObjectMove(0,name,1,t2,p2);
     }
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_FILL,true);
   ObjectSetInteger(0,name,OBJPROP_BACK,true);
   ObjectSetInteger(0,name,OBJPROP_WIDTH,1);
   StyleObject(name);
  }

void PutSegment(const string name,const datetime t1,const datetime t2,const double price,
                const color clr,const ENUM_LINE_STYLE style,const int width)
  {
   if(ObjectFind(0,name)<0)
      ObjectCreate(0,name,OBJ_TREND,0,t1,price,t2,price);
   else
     {
      ObjectMove(0,name,0,t1,price);
      ObjectMove(0,name,1,t2,price);
     }
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_STYLE,style);
   ObjectSetInteger(0,name,OBJPROP_WIDTH,width);
   ObjectSetInteger(0,name,OBJPROP_RAY_RIGHT,false);
   ObjectSetInteger(0,name,OBJPROP_BACK,false);
   StyleObject(name);
  }

void PutText(const string name,const datetime t,const double price,const string txt,
             const color clr,const int size,const ENUM_ANCHOR_POINT anchor)
  {
   if(ObjectFind(0,name)<0)
      ObjectCreate(0,name,OBJ_TEXT,0,t,price);
   else
      ObjectMove(0,name,0,t,price);
   ObjectSetString(0,name,OBJPROP_TEXT,txt);
   ObjectSetString(0,name,OBJPROP_FONT,"Arial");
   ObjectSetInteger(0,name,OBJPROP_FONTSIZE,size);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_ANCHOR,anchor);
   StyleObject(name);
  }

void PutPanelLine(const string name,const int x,const int y,const string txt,const color clr)
  {
   if(ObjectFind(0,name)<0)
      ObjectCreate(0,name,OBJ_LABEL,0,0,0);
   ObjectSetInteger(0,name,OBJPROP_CORNER,CORNER_LEFT_UPPER);
   ObjectSetInteger(0,name,OBJPROP_XDISTANCE,x);
   ObjectSetInteger(0,name,OBJPROP_YDISTANCE,y);
   ObjectSetString(0,name,OBJPROP_TEXT,txt);
   ObjectSetString(0,name,OBJPROP_FONT,"Consolas");
   ObjectSetInteger(0,name,OBJPROP_FONTSIZE,9);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   StyleObject(name);
  }

//+------------------------------------------------------------------+
//| Analysis                                                          |
//+------------------------------------------------------------------+
void AddSwing(const int idx,const double price,const bool isHigh)
  {
   int sz=ArraySize(g_swings);
   ArrayResize(g_swings,sz+1);
   g_swings[sz].idx=idx;
   g_swings[sz].price=price;
   g_swings[sz].isHigh=isHigh;
  }

void AddFvg(const int formed,const double top,const double bottom,const bool bullish,
            const int n,const double &close[])
  {
   FvgZone z;
   z.formed=formed;
   z.top=top;
   z.bottom=bottom;
   z.bullish=bullish;
   z.mitigated=n;
   for(int k=formed+1; k<n; k++)
     {
      if(bullish && close[k]<bottom) { z.mitigated=k; break; }
      if(!bullish && close[k]>top)   { z.mitigated=k; break; }
     }
   int sz=ArraySize(g_fvg);
   ArrayResize(g_fvg,sz+1);
   g_fvg[sz]=z;
  }

void BuildAtr(const int n,const double &high[],const double &low[],const double &close[])
  {
   int period=InpAtrPeriod;
   if(period<2) period=2;
   ArrayResize(g_atr,n);
   double sum=0.0;
   for(int i=0; i<n; i++)
     {
      double tr=high[i]-low[i];
      if(i>0)
        {
         double a=MathAbs(high[i]-close[i-1]);
         double b=MathAbs(low[i]-close[i-1]);
         if(a>tr) tr=a;
         if(b>tr) tr=b;
        }
      sum+=tr;
      if(i>=period)
        {
         double ptr=high[i-period]-low[i-period];
         if(i-period>0)
           {
            double a=MathAbs(high[i-period]-close[i-period-1]);
            double b=MathAbs(low[i-period]-close[i-period-1]);
            if(a>ptr) ptr=a;
            if(b>ptr) ptr=b;
           }
         sum-=ptr;
         g_atr[i]=sum/period;
        }
      else
         g_atr[i]=sum/(i+1);
     }
  }

//--- one setup: sweep of the liquidity + tap inside the FVG + reclaim
bool TryBuildSetup(const bool bullish,const int k,
                   const double lqLevel,const int lqIdx,
                   const double bosLevel,const int bosIdx,
                   const int n,const datetime &time[],
                   const double &high[],const double &low[],const double &close[],
                   TradeSetup &out)
  {
//--- 2) liquidity: the bar must run through the last swing level
   if(bullish)
     {
      if(bosLevel<=lqLevel) return(false);
      if(low[k]>=lqLevel)   return(false);
     }
   else
     {
      if(bosLevel>=lqLevel) return(false);
      if(high[k]<=lqLevel)  return(false);
     }

//--- 1) fair value gap: the sweeping wick has to land inside an unfilled gap
   double probe = bullish ? low[k] : high[k];
   int    fi=-1;
   for(int i=ArraySize(g_fvg)-1; i>=0; i--)
     {
      if(g_fvg[i].bullish!=bullish)    continue;
      if(g_fvg[i].formed>k-1)          continue;
      if(g_fvg[i].mitigated<=k)        continue;
      if(probe<g_fvg[i].bottom || probe>g_fvg[i].top) continue;
      if(InpRequireCE)
        {
         double mid=(g_fvg[i].top+g_fvg[i].bottom)*0.5;
         if(bullish  && probe>mid) continue;
         if(!bullish && probe<mid) continue;
        }
      fi=i;
      break;
     }
   if(fi<0)
      return(false);

   double height=g_fvg[fi].top-g_fvg[fi].bottom;
   if(height<=0.0)
      return(false);
   double buffer=height*InpBufferPercent/100.0;

//--- entry trigger: price reclaims the swept level within InpReclaimBars
   int last=k+InpReclaimBars;
   if(last>n-1)
      last=n-1;
   int    r=-1;
   double ext=probe;                       // furthest point reached during the sweep
   for(int i=k; i<=last; i++)
     {
      if(bullish)
        {
         if(low[i]<ext) ext=low[i];
         if(close[i]>lqLevel) { r=i; break; }
        }
      else
        {
         if(high[i]>ext) ext=high[i];
         if(close[i]<lqLevel) { r=i; break; }
        }
     }
   if(r<0)
      return(false);
//--- invalidated if price blew through the gap before reclaiming
   if(bullish  && ext<g_fvg[fi].bottom-buffer) return(false);
   if(!bullish && ext>g_fvg[fi].top+buffer)    return(false);

//--- 3) structure break level = target
   double entry=lqLevel;
   double sl,tp;
   if(bullish)
     {
      double base=MathMin(g_fvg[fi].bottom,ext);
      sl=base-buffer;
      tp=bosLevel+buffer;
      if(tp<=entry || sl>=entry) return(false);
     }
   else
     {
      double base=MathMax(g_fvg[fi].top,ext);
      sl=base+buffer;
      tp=bosLevel-buffer;
      if(tp>=entry || sl<=entry) return(false);
     }

   double risk   = bullish ? entry-sl : sl-entry;
   double reward = bullish ? tp-entry : entry-tp;
   if(risk<=0.0 || reward<=0.0)
      return(false);
   if(InpMinRR>0.0 && reward/risk<InpMinRR)
      return(false);

   double tp1 = bullish ? entry+(tp-entry)*InpTP1Percent/100.0
                        : entry-(entry-tp)*InpTP1Percent/100.0;

//--- outcome of the trade (stop checked first, conservative)
   int res=0, resIdx=-1;
   for(int i=r; i<n; i++)
     {
      if(bullish)
        {
         if(low[i]<=sl)  { res=-1; resIdx=i; break; }
         if(high[i]>=tp) { res= 1; resIdx=i; break; }
        }
      else
        {
         if(high[i]>=sl) { res=-1; resIdx=i; break; }
         if(low[i]<=tp)  { res= 1; resIdx=i; break; }
        }
     }

   out.bullish   = bullish;
   out.sweepIdx  = k;
   out.entryIdx  = r;
   out.lqIdx     = lqIdx;
   out.bosIdx    = bosIdx;
   out.fvgIdx    = fi;
   out.lq        = lqLevel;
   out.bos       = bosLevel;
   out.entry     = entry;
   out.sl        = sl;
   out.tp1       = tp1;
   out.tp        = tp;
   out.fvgTop    = g_fvg[fi].top;
   out.fvgBottom = g_fvg[fi].bottom;
   out.result    = res;
   out.resultIdx = resIdx;
   out.entryTime = time[r];
   return(true);
  }

void Analyse(const int n,const datetime &time[],const double &high[],
             const double &low[],const double &close[])
  {
   int w=InpSwingWindow;
   if(w<1) w=1;
   int lookback=InpLookbackBars;
   if(lookback<50) lookback=50;
   int start=n-lookback;
   if(start<0) start=0;

   ArrayResize(g_swings,0);
   ArrayResize(g_fvg,0);
   ArrayResize(g_setups,0);
   g_curLq=0.0; g_curBos=0.0; g_curLqIdx=-1; g_curBosIdx=-1;

   BuildAtr(n,high,low,close);

//--- swing highs / lows (fractals)
   for(int i=start+w; i<n-w; i++)
     {
      bool isHigh=true, isLow=true;
      for(int j=i-w; j<=i+w; j++)
        {
         if(j==i) continue;
         if(high[j]>high[i]) isHigh=false;
         if(low[j]<low[i])   isLow=false;
        }
      if(isHigh) AddSwing(i,high[i],true);
      if(isLow)  AddSwing(i,low[i],false);
     }

//--- fair value gaps (3-candle imbalance), only the ones worth trading
   for(int j=start+2; j<n; j++)
     {
      double minH=g_atr[j]*InpMinFvgAtrPct/100.0;
      if(low[j]>high[j-2] && (low[j]-high[j-2])>=minH)
         AddFvg(j,low[j],high[j-2],true,n,close);
      if(high[j]<low[j-2] && (low[j-2]-high[j])>=minH)
         AddFvg(j,low[j-2],high[j],false,n,close);
     }

//--- walk forward, keeping the last confirmed structure, and test each bar
   int    ns=ArraySize(g_swings);
   int    p=0;
   double curHigh=0.0, curLow=0.0;
   int    curHighIdx=-1, curLowIdx=-1;
   int    usedBuyLq=-1, usedSellLq=-1;
   TradeSetup st;

   for(int k=start; k<n; k++)
     {
      while(p<ns && g_swings[p].idx+w<=k)
        {
         if(g_swings[p].isHigh) { curHigh=g_swings[p].price; curHighIdx=g_swings[p].idx; }
         else                   { curLow =g_swings[p].price; curLowIdx =g_swings[p].idx; }
         p++;
        }

      if(InpAllowBuys && curLowIdx>=0 && curHighIdx>=0 && curLowIdx!=usedBuyLq)
        {
         if(TryBuildSetup(true,k,curLow,curLowIdx,curHigh,curHighIdx,n,time,high,low,close,st))
           {
            int sz=ArraySize(g_setups);
            ArrayResize(g_setups,sz+1);
            g_setups[sz]=st;
            usedBuyLq=curLowIdx;
           }
        }

      if(InpAllowSells && curLowIdx>=0 && curHighIdx>=0 && curHighIdx!=usedSellLq)
        {
         if(TryBuildSetup(false,k,curHigh,curHighIdx,curLow,curLowIdx,n,time,high,low,close,st))
           {
            int sz=ArraySize(g_setups);
            ArrayResize(g_setups,sz+1);
            g_setups[sz]=st;
            usedSellLq=curHighIdx;
           }
        }
     }

   g_curLq=curLow;   g_curLqIdx=curLowIdx;
   g_curBos=curHigh; g_curBosIdx=curHighIdx;

   g_validFvg=0;
   for(int i=0; i<ArraySize(g_fvg); i++)
      if(g_fvg[i].mitigated>=n)
         g_validFvg++;
  }

//+------------------------------------------------------------------+
//| Drawing                                                           |
//+------------------------------------------------------------------+
datetime BarTime(const int idx,const int n,const datetime &time[])
  {
   if(idx<0)
      return(time[0]);
   if(idx<n)
      return(time[idx]);
   return(time[n-1]+(datetime)(PeriodSeconds()*(idx-(n-1))));
  }

void DrawFvgBox(const int fvgIdx,const int n,const datetime &time[],const datetime rightEdge)
  {
   if(fvgIdx<0 || fvgIdx>=ArraySize(g_fvg))
      return;
   string id=PREFIX+"FVG"+IntegerToString(g_fvg[fvgIdx].formed);
   int leftIdx=g_fvg[fvgIdx].formed-2;
   if(leftIdx<0) leftIdx=0;
   datetime t1=BarTime(leftIdx,n,time);
   PutBox(id,t1,g_fvg[fvgIdx].top,rightEdge,g_fvg[fvgIdx].bottom,c_fvgFill);
   double mid=(g_fvg[fvgIdx].top+g_fvg[fvgIdx].bottom)*0.5;
   PutSegment(id+"m",t1,rightEdge,mid,c_fvgMid,STYLE_DASH,1);
   PutText(id+"t",t1,mid,"FVG",c_fvgText,8,ANCHOR_LEFT);
  }

void DrawSetup(const int slot,const TradeSetup &st,const int n,
               const datetime &time[],const datetime rightEdge)
  {
   string id=PREFIX+"S"+IntegerToString(slot)+"_";
   int width=InpTradeBoxBars;
   if(width<1) width=1;
   datetime t0=BarTime(st.entryIdx,n,time);
   datetime t1=BarTime(st.entryIdx+width,n,time);

//--- the conditions that produced this trade
   DrawFvgBox(st.fvgIdx,n,time,rightEdge);
   PutSegment(id+"lq",BarTime(st.lqIdx,n,time),rightEdge,st.lq,c_level,STYLE_DOT,1);
   PutText(id+"lqT",rightEdge,st.lq,"LQ  "+Px(st.lq),c_label,8,ANCHOR_RIGHT_UPPER);
   PutSegment(id+"bos",BarTime(st.bosIdx,n,time),rightEdge,st.bos,c_level,STYLE_DOT,1);
   PutText(id+"bosT",rightEdge,st.bos,"BOS  "+Px(st.bos),c_label,8,ANCHOR_RIGHT_UPPER);

//--- the trade itself (green = target zone, red = risk zone)
   PutBox(id+"win",t0,st.entry,t1,st.tp,c_profit);
   PutBox(id+"loss",t0,st.entry,t1,st.sl,c_risk);
   PutSegment(id+"entry",t0,t1,st.entry,c_entry,STYLE_DOT,1);
   PutSegment(id+"tp1",t0,t1,st.tp1,c_tp,STYLE_DOT,1);
   PutSegment(id+"tp",t0,t1,st.tp,c_tp,STYLE_DOT,1);
   PutSegment(id+"sl",t0,t1,st.sl,c_sl,STYLE_DOT,1);

   string side=st.bullish ? "BUY" : "SELL";
   PutText(id+"eT",t1,st.entry,side+"  "+Px(st.entry),c_entry,8,ANCHOR_LEFT);
   PutText(id+"t1T",t1,st.tp1,"TP #1  "+Px(st.tp1),c_label,8,ANCHOR_LEFT);
   PutText(id+"tT",t1,st.tp,"TP  "+Px(st.tp),c_tp,8,ANCHOR_LEFT);
   PutText(id+"sT",t1,st.sl,"SL  "+Px(st.sl),c_sl,8,ANCHOR_LEFT);

//--- result, the way the video ends with "378 PIPS"
   double risk   = st.bullish ? st.entry-st.sl : st.sl-st.entry;
   double reward = st.bullish ? st.tp-st.entry : st.entry-st.tp;
   string rr=DoubleToString(reward/risk,2)+"R";
   string res;
   color  rc;
   if(st.result>0)      { res="+"+PipsText(reward)+" PIPS   "+rr; rc=c_win;  }
   else if(st.result<0) { res="-"+PipsText(risk)+" PIPS   "+rr;   rc=c_loss; }
   else                 { res="running   "+rr;                    rc=c_open; }
   double labelPrice = st.bullish ? st.sl-risk*0.35 : st.sl+risk*0.35;
   PutText(id+"res",t0,labelPrice,res,rc,10,ANCHOR_LEFT);
  }

void DrawPanel()
  {
   int total=ArraySize(g_setups);
   PutPanelLine(PREFIX+"p0",10,20,"SMC video strategy",c_level);
   PutPanelLine(PREFIX+"p1",10,38,"1. FVG zones valid : "+IntegerToString(g_validFvg),c_fvgText);
   PutPanelLine(PREFIX+"p2",10,54,"2. Liquidity (LQ)  : "+(g_curLqIdx>=0?Px(g_curLq):"-"),c_label);
   PutPanelLine(PREFIX+"p3",10,70,"3. Structure (BOS) : "+(g_curBosIdx>=0?Px(g_curBos):"-"),c_label);
   string last="none";
   if(total>0)
     {
      TradeSetup s=g_setups[total-1];
      last=(s.bullish?"BUY ":"SELL ")+Px(s.entry)+"  SL "+Px(s.sl)+"  TP "+Px(s.tp);
     }
   PutPanelLine(PREFIX+"p4",10,88,"Last setup : "+last,c_level);
  }

void Redraw(const int n,const datetime &time[],const double &close[])
  {
   ObjectsDeleteAll(0,PREFIX);
   datetime rightEdge=time[n-1]+(datetime)(PeriodSeconds()*8);

//--- the trades: newest first, one by default (exactly like the video)
   int total=ArraySize(g_setups);
   int shown=InpMaxSetups;
   if(shown<0) shown=0;
   int from=total-shown;
   if(from<0) from=0;
   for(int s=from; s<total; s++)
      DrawSetup(s,g_setups[s],n,time,rightEdge);

//--- the nearest valid gaps that are still in play (condition 1)
   if(InpShowFvgBoxes && InpMaxFvgBoxes>0)
     {
      double price=close[n-1];
      double reach=g_atr[n-1]*20.0;
      int    drawn=0;
      for(int i=ArraySize(g_fvg)-1; i>=0 && drawn<InpMaxFvgBoxes; i--)
        {
         if(g_fvg[i].mitigated<n)                 continue;   // already filled
         if(n-1-g_fvg[i].formed>InpFvgMaxAgeBars) continue;   // too old
         double mid=(g_fvg[i].top+g_fvg[i].bottom)*0.5;
         if(MathAbs(price-mid)>reach)             continue;   // far away from price
         DrawFvgBox(i,n,time,rightEdge);
         drawn++;
        }
     }

//--- current liquidity and structure levels (conditions 2 and 3),
//--- skipped when the drawn trade already shows the same levels
   if(InpShowLevels)
     {
      bool lqShown=false, bosShown=false;
      for(int s=from; s<total; s++)
        {
         if(g_setups[s].lqIdx==g_curLqIdx)   lqShown=true;
         if(g_setups[s].bosIdx==g_curBosIdx) bosShown=true;
         if(g_setups[s].lqIdx==g_curBosIdx)  bosShown=true;
         if(g_setups[s].bosIdx==g_curLqIdx)  lqShown=true;
        }
      if(g_curLqIdx>=0 && !lqShown)
        {
         PutSegment(PREFIX+"curLQ",BarTime(g_curLqIdx,n,time),rightEdge,g_curLq,c_level,STYLE_DOT,1);
         PutText(PREFIX+"curLQt",rightEdge,g_curLq,"LQ  "+Px(g_curLq),c_label,8,ANCHOR_RIGHT_UPPER);
        }
      if(g_curBosIdx>=0 && !bosShown)
        {
         PutSegment(PREFIX+"curBOS",BarTime(g_curBosIdx,n,time),rightEdge,g_curBos,c_level,STYLE_DOT,1);
         PutText(PREFIX+"curBOSt",rightEdge,g_curBos,"BOS  "+Px(g_curBos),c_label,8,ANCHOR_RIGHT_UPPER);
        }
     }

   if(InpShowPanel)
      DrawPanel();
  }

void CheckAlerts(const int n)
  {
   int total=ArraySize(g_setups);
   if(total<=0)
      return;
   TradeSetup s=g_setups[total-1];
   if(s.entryIdx<n-2)                 // only a setup that just triggered
      return;
   if(s.entryTime<=g_lastAlertTime)
      return;
   g_lastAlertTime=s.entryTime;
   string msg=StringFormat("SMC setup  %s %s  %s @ %s   SL %s   TP %s",
                           _Symbol,EnumToString((ENUM_TIMEFRAMES)_Period),
                           (s.bullish?"BUY":"SELL"),
                           Px(s.entry),Px(s.sl),Px(s.tp));
   if(InpAlertPopup)
      Alert(msg);
   if(InpAlertPush)
      SendNotification(msg);
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
   if(rates_total<InpSwingWindow*2+20)
      return(rates_total);

   ArraySetAsSeries(time,false);
   ArraySetAsSeries(open,false);
   ArraySetAsSeries(high,false);
   ArraySetAsSeries(low,false);
   ArraySetAsSeries(close,false);

//--- one full pass per closed bar is enough
   if(prev_calculated>0 && time[rates_total-1]==g_lastBarTime)
      return(rates_total);
   g_lastBarTime=time[rates_total-1];

   Analyse(rates_total,time,high,low,close);
   Redraw(rates_total,time,close);
   CheckAlerts(rates_total);
   ChartRedraw();
   return(rates_total);
  }
//+------------------------------------------------------------------+

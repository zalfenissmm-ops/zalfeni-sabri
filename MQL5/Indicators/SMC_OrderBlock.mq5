//+------------------------------------------------------------------+
//|                                              SMC_OrderBlock.mq5  |
//|          Order Block (ICT / SMC) — كشف ورسم الأوردر بلوك الصحيح   |
//+------------------------------------------------------------------+
//  الـ OB الصحيح = آخر شمعة معاكسة قبل الـ displacement اللي كسّر الستركتشر
//  الشروط (كلهم مع بعض):
//    1) Liquidity sweep قبلها (كنس high/low واضح أو equal highs/lows)
//    2) Displacement — حركة قوية بشمعات كبار، موش تدريجية
//    3) BOS / MSS — الحركة تكسّر الستركتشر فعلاً
//    4) FVG — الحركة تخلّي imbalance وراها
//    5) Unmitigated — السعر مازال ما رجعلهاش
//+------------------------------------------------------------------+
#property copyright   "zalfeni-sabri"
#property version     "1.00"
#property description "Order Block detector: Sweep + Displacement + BOS/MSS + FVG + Unmitigated"
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

//+------------------------------------------------------------------+
//| Enums                                                            |
//+------------------------------------------------------------------+
enum ENUM_OB_RANGE
  {
   OB_RANGE_OPEN_WICK = 0,  // Open -> Wick  (التعريف الأصلي)
   OB_RANGE_FULL      = 1,  // High -> Low   (الشمعة كاملة)
   OB_RANGE_BODY      = 2   // Body فقط      (Open -> Close)
  };

enum ENUM_MITIGATION
  {
   MIT_TOUCH_EDGE = 0,      // لمسة الحافة القريبة
   MIT_TOUCH_CE   = 1,      // الوصول لـ CE 50%
   MIT_CLOSE_IN   = 2       // إغلاق داخل الزون
  };

enum ENUM_STRENGTH_FILTER
  {
   SF_ALL    = 0,           // اعرض الكل
   SF_B_PLUS = 1,           // B وأقوى
   SF_A_ONLY = 2            // A / A+ فقط
  };

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group                "=== 1) الاكتشاف ==="
input ENUM_TIMEFRAMES      InpDetectTF        = PERIOD_CURRENT; // تايم فريم الاكتشاف (H1/H4 مثلا)
input int                  InpBarsToScan      = 1500;           // عدد الشموع للفحص
input int                  InpSwingWindow     = 3;              // نافذة القمم/القيعان (fractal)
input int                  InpMaxOB           = 8;              // أقصى عدد OB معروضة
input int                  InpMaxOBCandles    = 3;              // أقصى شموع معاكسة في الكتلة
input ENUM_OB_RANGE        InpOBRange         = OB_RANGE_OPEN_WICK; // مدى الزون
input int                  InpMaxLegBars      = 12;             // أقصى طول للرجوع لور من الكسر
input int                  InpAvgPeriod       = 14;             // فترة متوسط المدى (مرجع القوة)

input group                "=== 2) الشروط ==="
input bool                 InpRequireSweep    = true;           // 1) لازم Liquidity sweep
input int                  InpSweepLookback   = 12;             // نافذة البحث على الـ sweep
input double               InpEqualTolerance  = 0.15;           // تفاوت equal highs/lows (× متوسط المدى)
input bool                 InpRequireDisp     = true;           // 2) لازم Displacement
input double               InpDispMult        = 1.8;            // قوة الحركة (× متوسط المدى)
input double               InpMinBodyRatio    = 0.50;           // نسبة الأجسام في الحركة (موش فتايل)
input int                  InpMaxDispBars     = 5;              // أقصى شموع في الحركة (موش تدريجية)
input bool                 InpRequireMSS      = false;          // 3) MSS فقط (لا تقبل BOS عادي)
input bool                 InpRequireFVG      = true;           // 4) لازم FVG داخل الحركة
input double               InpMinFVGRatio     = 0.10;           // أدنى حجم FVG (× متوسط المدى)
input bool                 InpHideMitigated   = false;          // 5) اخفي الـ OB المستهلكة
input ENUM_MITIGATION      InpMitigation      = MIT_TOUCH_EDGE; // تعريف الـ mitigation
input bool                 InpDeleteBroken    = true;           // امسح الزون إذا انكسرت (إغلاق وراها)
input bool                 InpRequireDiscount = false;          // لازم تكون في premium/discount الصحيح
input int                  InpRangeLookback   = 60;             // نافذة الرينج لحساب premium/discount
input ENUM_STRENGTH_FILTER InpMinStrength     = SF_ALL;         // فلتر القوة

input group                "=== 3) Refinement (تايم فريم أصغر) ==="
input bool                 InpRefine          = true;           // فعّل الـ refinement داخل الزون
input ENUM_TIMEFRAMES      InpRefineTF        = PERIOD_M15;     // تايم فريم الـ refine (لازم أصغر)
input color                InpRefineColor     = clrOrange;      // لون الزون المكرّرة

input group                "=== 4) الرسم ==="
input color                InpBullColor       = C'0,150,120';   // لون OB صاعد
input color                InpBearColor       = C'200,60,80';   // لون OB هابط
input color                InpBullMitColor    = C'95,115,110';  // لون OB صاعد مستهلك
input color                InpBearMitColor    = C'125,95,100';  // لون OB هابط مستهلك
input bool                 InpFillZones       = true;           // تعبئة الزون
input int                  InpBorderWidth     = 1;              // سُمك الإطار (0 = بلا إطار)
input bool                 InpShowCE          = true;           // ارسم خط CE 50%
input color                InpCEColor         = clrSilver;      // لون CE
input bool                 InpShowFVG         = true;           // ارسم الـ FVG
input color                InpFVGColor        = C'110,110,190';  // لون FVG
input bool                 InpShowSweepLine   = true;           // ارسم مستوى السيولة المكنوسة
input color                InpSweepColor      = clrGoldenrod;   // لون السيولة
input bool                 InpShowOTE         = false;          // ارسم OTE 62-79%
input color                InpOTEColor        = C'150,120,60';  // لون OTE
input bool                 InpShowLabels      = true;           // اعرض التسميات
input color                InpTextColor       = clrSilver;      // لون النص
input int                  InpFontSize        = 8;              // حجم الخط
input int                  InpExtendBars      = 30;             // تمديد الزون لليمين (شموع)

input group                "=== 5) التنبيهات ==="
input bool                 InpAlertNewOB      = false;          // نبّه عند تكوّن OB جديدة
input bool                 InpAlertTap        = false;          // نبّه عند دخول السعر للزون
input bool                 InpAlertPopup      = true;           // نافذة تنبيه
input bool                 InpAlertPush       = false;          // إشعار للموبايل
input bool                 InpAlertMail       = false;          // إيميل
input bool                 InpPrintLog        = false;          // اطبع تفاصيل الكشف في اللوج

//+------------------------------------------------------------------+
//| Structures                                                       |
//+------------------------------------------------------------------+
struct OBZone
  {
   bool              bullish;        // اتجاه الزون
   int               startIdx;       // أول شمعة في الكتلة
   int               endIdx;         // آخر شمعة في الكتلة
   datetime          startTime;
   datetime          endTime;
   double            top;
   double            bottom;
   double            ce;             // 50%
   //--- structure
   int               breakIdx;
   datetime          breakTime;
   double            breakLevel;
   bool              isMSS;          // true = MSS/CHoCH ، false = BOS
   //--- sweep
   bool              hasSweep;
   bool              sweepEqual;     // كنس equal highs/lows
   double            sweepLevel;
   datetime          sweepTime;
   //--- displacement
   bool              hasDisp;
   double            dispRatio;
   double            bodyRatio;
   int               dispBars;
   //--- fvg
   bool              hasFVG;
   double            fvgTop;
   double            fvgBottom;
   datetime          fvgT1;
   datetime          fvgT2;
   //--- state
   bool              mitigated;
   datetime          mitTime;
   bool              broken;
   //--- context
   bool              inZone;         // premium/discount صحيح
   double            rangePos;       // 0..1
   bool              hasOTE;
   double            oteTop;
   double            oteBottom;
   //--- refinement
   bool              refined;
   double            refTop;
   double            refBottom;
   //--- score
   int               score;
   string            grade;
   string            tags;
  };

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
const string      g_prefix = "SMCOB_";
ENUM_TIMEFRAMES   g_detTF  = PERIOD_CURRENT;
MqlRates          g_rates[];
int               g_n = 0;
bool              g_isSH[];
bool              g_isSL[];
int               g_shIdx[];
double            g_shPx[];
int               g_shN = 0;
int               g_slIdx[];
double            g_slPx[];
int               g_slN = 0;
OBZone            g_obs[];
int               g_obN = 0;
datetime          g_lastBar = 0;
datetime          g_alertForm[];
datetime          g_alertTap[];

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
string TFName(const ENUM_TIMEFRAMES tf)
  {
   string s = EnumToString(tf);
   if(StringSubstr(s, 0, 7) == "PERIOD_")
      s = StringSubstr(s, 7);
   return(s);
  }

double AvgRange(const int endIdx, const int period)
  {
   int p    = (int)MathMax(3, period);
   int from = (int)MathMax(0, endIdx - p + 1);
   double s = 0.0;
   int    c = 0;
   for(int i = from; i <= endIdx && i < g_n; i++)
     {
      s += (g_rates[i].high - g_rates[i].low);
      c++;
     }
   if(c == 0)
      return(0.0);
   double a = s / c;
   if(a <= 0.0)
      a = _Point;
   return(a);
  }

double HighestHigh(const int from, const int to)
  {
   double v = -DBL_MAX;
   for(int i = (int)MathMax(0, from); i <= to && i < g_n; i++)
      if(g_rates[i].high > v)
         v = g_rates[i].high;
   return(v);
  }

double LowestLow(const int from, const int to)
  {
   double v = DBL_MAX;
   for(int i = (int)MathMax(0, from); i <= to && i < g_n; i++)
      if(g_rates[i].low < v)
         v = g_rates[i].low;
   return(v);
  }

bool IsWithMove(const int i, const bool bull)
  {
   if(bull)
      return(g_rates[i].close >= g_rates[i].open);
   return(g_rates[i].close <= g_rates[i].open);
  }

bool IsOpposite(const int i, const bool bull)
  {
   if(bull)
      return(g_rates[i].close < g_rates[i].open);   // شمعة هابطة قبل حركة صعود
   return(g_rates[i].close > g_rates[i].open);      // شمعة صاعدة قبل حركة هبوط
  }

bool Overlap(const double a1, const double a2, const double b1, const double b2)
  {
   double aLo = MathMin(a1, a2), aHi = MathMax(a1, a2);
   double bLo = MathMin(b1, b2), bHi = MathMax(b1, b2);
   return(aLo <= bHi && bLo <= aHi);
  }

//+------------------------------------------------------------------+
//| Init / Deinit                                                    |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_detTF = (InpDetectTF == PERIOD_CURRENT) ? (ENUM_TIMEFRAMES)_Period : InpDetectTF;
   IndicatorSetString(INDICATOR_SHORTNAME, "SMC OrderBlock [" + TFName(g_detTF) + "]");
   ObjectsDeleteAll(0, g_prefix);
   ArrayResize(g_alertForm, 0);
   ArrayResize(g_alertTap, 0);
   g_lastBar = 0;
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, g_prefix);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Main                                                             |
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
   if(rates_total < 50)
      return(rates_total);

   datetime cur = (datetime)SeriesInfoInteger(_Symbol, g_detTF, SERIES_LASTBAR_DATE);
   if(prev_calculated <= 0 || cur != g_lastBar || cur == 0)
     {
      g_lastBar = cur;
      Recompute();
     }
   CheckTapAlerts();
   return(rates_total);
  }

//+------------------------------------------------------------------+
//| Recompute everything                                             |
//+------------------------------------------------------------------+
void Recompute()
  {
   ObjectsDeleteAll(0, g_prefix);

   int want = (int)MathMax(200, InpBarsToScan);
   ArraySetAsSeries(g_rates, false);
   g_n = CopyRates(_Symbol, g_detTF, 0, want, g_rates);
   if(g_n < 50)
     {
      g_obN = 0;
      ArrayResize(g_obs, 0);
      return;
     }

   FindSwings();

   g_obN = 0;
   ArrayResize(g_obs, 0);
   ScanStructure();
   TrimOBs();

   if(InpRefine)
      RefineAll();

   DrawAll();
   ChartRedraw();

   if(InpAlertNewOB)
      CheckFormAlerts();
  }

//+------------------------------------------------------------------+
//| Swing points (fractals)                                          |
//+------------------------------------------------------------------+
void FindSwings()
  {
   int k = (int)MathMax(1, InpSwingWindow);

   ArrayResize(g_isSH, g_n);
   ArrayResize(g_isSL, g_n);
   for(int i = 0; i < g_n; i++)
     {
      g_isSH[i] = false;
      g_isSL[i] = false;
     }

   ArrayResize(g_shIdx, g_n);
   ArrayResize(g_shPx,  g_n);
   ArrayResize(g_slIdx, g_n);
   ArrayResize(g_slPx,  g_n);
   g_shN = 0;
   g_slN = 0;

   for(int i = k; i < g_n - k; i++)
     {
      bool hi = true, lo = true;
      for(int j = 1; j <= k; j++)
        {
         if(g_rates[i].high <  g_rates[i - j].high) hi = false;
         if(g_rates[i].high <= g_rates[i + j].high) hi = false;
         if(g_rates[i].low  >  g_rates[i - j].low)  lo = false;
         if(g_rates[i].low  >= g_rates[i + j].low)  lo = false;
         if(!hi && !lo)
            break;
        }
      if(hi)
        {
         g_isSH[i] = true;
         g_shIdx[g_shN] = i;
         g_shPx[g_shN]  = g_rates[i].high;
         g_shN++;
        }
      if(lo)
        {
         g_isSL[i] = true;
         g_slIdx[g_slN] = i;
         g_slPx[g_slN]  = g_rates[i].low;
         g_slN++;
        }
     }
  }

//+------------------------------------------------------------------+
//| Structure scan: BOS / MSS -> build OB                            |
//+------------------------------------------------------------------+
void ScanStructure()
  {
   int k = (int)MathMax(1, InpSwingWindow);

   int    actHi = -1, actLo = -1;
   double actHiPx = 0.0, actLoPx = 0.0;
   int    bias = 0;

   for(int i = k; i < g_n; i++)
     {
      int c = i - k;                       // swing يتأكد بعد k شمعة
      if(c >= 0 && c < g_n)
        {
         if(g_isSH[c]) { actHi = c; actHiPx = g_rates[c].high; }
         if(g_isSL[c]) { actLo = c; actLoPx = g_rates[c].low;  }
        }

      if(actHi >= 0 && i > actHi && g_rates[i].close > actHiPx)
        {
         bool mss = (bias == -1);
         bias = 1;
         OBZone ob;
         if(BuildOB(i, true, mss, actHiPx, ob))
            AddOB(ob);
         actHi = -1;
        }

      if(actLo >= 0 && i > actLo && g_rates[i].close < actLoPx)
        {
         bool mss = (bias == 1);
         bias = -1;
         OBZone ob;
         if(BuildOB(i, false, mss, actLoPx, ob))
            AddOB(ob);
         actLo = -1;
        }
     }
  }

void AddOB(OBZone &ob)
  {
   ArrayResize(g_obs, g_obN + 1);
   g_obs[g_obN] = ob;
   g_obN++;
  }

void TrimOBs()
  {
   int keep = (int)MathMax(1, InpMaxOB);
   if(g_obN <= keep)
      return;
   int start = g_obN - keep;
   for(int i = 0; i < keep; i++)
      g_obs[i] = g_obs[start + i];
   g_obN = keep;
   ArrayResize(g_obs, g_obN);
  }

//+------------------------------------------------------------------+
//| Build one Order Block from a structure break                     |
//|  breakIdx : الشمعة اللي سكّرت وراء مستوى الستركتشر                |
//|  bull     : true = حركة صعود  ->  OB من شمعة/شمعات هابطة          |
//+------------------------------------------------------------------+
bool BuildOB(const int breakIdx, const bool bull, const bool isMSS,
             const double breakLevel, OBZone &ob)
  {
   //--- init
   ob.bullish    = bull;
   ob.breakIdx   = breakIdx;
   ob.breakTime  = g_rates[breakIdx].time;
   ob.breakLevel = breakLevel;
   ob.isMSS      = isMSS;
   ob.hasSweep   = false;  ob.sweepEqual = false;
   ob.sweepLevel = 0.0;    ob.sweepTime  = 0;
   ob.hasDisp    = false;  ob.dispRatio  = 0.0;
   ob.bodyRatio  = 0.0;    ob.dispBars   = 0;
   ob.hasFVG     = false;  ob.fvgTop = 0.0; ob.fvgBottom = 0.0;
   ob.fvgT1      = 0;      ob.fvgT2 = 0;
   ob.mitigated  = false;  ob.mitTime = 0;  ob.broken = false;
   ob.inZone     = false;  ob.rangePos = 0.5;
   ob.hasOTE     = false;  ob.oteTop = 0.0; ob.oteBottom = 0.0;
   ob.refined    = false;  ob.refTop = 0.0; ob.refBottom = 0.0;
   ob.score      = 0;      ob.grade = "C";  ob.tags = "";

   //--- 3) MSS فقط إذا مطلوب
   if(InpRequireMSS && !isMSS)
      return(false);

   //--- ارجع من نقطة الكسر لور: أوّل شمعة معاكسة
   int j     = breakIdx;
   int guard = 0;
   while(j > 0 && IsWithMove(j, bull) && guard < InpMaxLegBars)
     {
      j--;
      guard++;
     }
   if(j == breakIdx)          // شمعة الكسر روحها معاكسة => ما فماش displacement نظيف
      return(false);
   if(!IsOpposite(j, bull))   // ما لقيناش شمعة معاكسة في المدى المسموح
      return(false);

   int obEnd   = j;
   int obStart = obEnd;
   int cnt     = 1;
   //--- شمعتين ولا ثلاثة معاكسين متتاليين = كتلة وحدة
   while(obStart > 0 && cnt < (int)MathMax(1, InpMaxOBCandles) && IsOpposite(obStart - 1, bull))
     {
      obStart--;
      cnt++;
     }

   ob.startIdx  = obStart;
   ob.endIdx    = obEnd;
   ob.startTime = g_rates[obStart].time;
   ob.endTime   = g_rates[obEnd].time;

   //--- مدى الزون
   double blkOpen  = g_rates[obStart].open;
   double blkClose = g_rates[obEnd].close;
   double blkHigh  = HighestHigh(obStart, obEnd);
   double blkLow   = LowestLow(obStart, obEnd);

   double top = 0.0, bottom = 0.0;
   if(InpOBRange == OB_RANGE_FULL)
     {
      top = blkHigh; bottom = blkLow;
     }
   else
      if(InpOBRange == OB_RANGE_BODY)
        {
         top    = MathMax(blkOpen, blkClose);
         bottom = MathMin(blkOpen, blkClose);
        }
      else // OB_RANGE_OPEN_WICK : من الـ open للفتيل
        {
         if(bull) { top = blkOpen; bottom = blkLow;  }
         else     { top = blkHigh; bottom = blkOpen; }
        }
   if(top <= bottom)          // حماية من حالات شاذة
     {
      top = blkHigh; bottom = blkLow;
     }
   if(top <= bottom)
      return(false);

   ob.top    = top;
   ob.bottom = bottom;
   ob.ce     = (top + bottom) * 0.5;

   //--- 2) Displacement : من بعد الـ OB لين شمعة الكسر
   int legStart = obEnd + 1;
   int legEnd   = breakIdx;
   int legBars  = legEnd - legStart + 1;
   if(legBars < 1)
      return(false);

   double avg = AvgRange(obEnd, InpAvgPeriod);
   double legHigh = HighestHigh(legStart, legEnd);
   double legLow  = LowestLow(legStart, legEnd);
   double bodySum = 0.0, rangeSum = 0.0;
   for(int b = legStart; b <= legEnd; b++)
     {
      bodySum  += MathAbs(g_rates[b].close - g_rates[b].open);
      rangeSum += (g_rates[b].high - g_rates[b].low);
     }
   ob.dispBars   = legBars;
   ob.dispRatio  = (avg > 0.0) ? (legHigh - legLow) / avg : 0.0;
   ob.bodyRatio  = (rangeSum > 0.0) ? bodySum / rangeSum : 0.0;
   ob.hasDisp    = (legBars <= (int)MathMax(1, InpMaxDispBars) &&
                    ob.dispRatio >= InpDispMult &&
                    ob.bodyRatio >= InpMinBodyRatio);
   if(InpRequireDisp && !ob.hasDisp)
     {
      if(InpPrintLog)
         PrintFormat("%s OB @%s رُفض: displacement ضعيف (bars=%d ratio=%.2f body=%.2f)",
                     (bull ? "BULL" : "BEAR"), TimeToString(ob.startTime), legBars,
                     ob.dispRatio, ob.bodyRatio);
      return(false);
     }

   //--- 4) FVG داخل الحركة
   double bestSize = 0.0;
   for(int m = legStart; m <= legEnd && m + 1 < g_n; m++)
     {
      if(m - 1 < 0)
         continue;
      double gTop = 0.0, gBot = 0.0;
      if(bull)
        {
         if(g_rates[m + 1].low > g_rates[m - 1].high)
           {
            gBot = g_rates[m - 1].high;
            gTop = g_rates[m + 1].low;
           }
        }
      else
        {
         if(g_rates[m + 1].high < g_rates[m - 1].low)
           {
            gTop = g_rates[m - 1].low;
            gBot = g_rates[m + 1].high;
           }
        }
      double sz = gTop - gBot;
      if(sz > 0.0 && sz > bestSize && (avg <= 0.0 || sz >= InpMinFVGRatio * avg))
        {
         bestSize     = sz;
         ob.hasFVG    = true;
         ob.fvgTop    = gTop;
         ob.fvgBottom = gBot;
         ob.fvgT1     = g_rates[m - 1].time;
         ob.fvgT2     = g_rates[(int)MathMin(m + 1, g_n - 1)].time;
        }
     }
   if(InpRequireFVG && !ob.hasFVG)
     {
      if(InpPrintLog)
         PrintFormat("%s OB @%s رُفض: ما فماش FVG في الحركة",
                     (bull ? "BULL" : "BEAR"), TimeToString(ob.startTime));
      return(false);
     }

   //--- 1) Liquidity sweep قبل الـ OB
   int wFrom = (int)MathMax((int)MathMax(1, InpSwingWindow), obStart - (int)MathMax(1, InpSweepLookback));
   for(int b = obEnd; b >= wFrom && !ob.hasSweep; b--)
     {
      if(bull)
        {
         for(int s = g_slN - 1; s >= 0; s--)
           {
            int si = g_slIdx[s];
            if(si >= b)
               continue;
            if(si + InpSwingWindow > b)      // مازال ما تأكدش وقت الكنس
               continue;
            if(b - si > InpSweepLookback + InpMaxOBCandles + InpSwingWindow)
               break;
            if(g_rates[b].low < g_slPx[s] && g_rates[b].close > g_slPx[s])
              {
               ob.hasSweep   = true;
               ob.sweepLevel = g_slPx[s];
               ob.sweepTime  = g_rates[si].time;
               for(int e = 0; e < g_slN; e++)
                  if(e != s && g_slIdx[e] < b &&
                     MathAbs(g_slPx[e] - g_slPx[s]) <= InpEqualTolerance * avg)
                    {
                     ob.sweepEqual = true;
                     break;
                    }
               break;
              }
           }
        }
      else
        {
         for(int s = g_shN - 1; s >= 0; s--)
           {
            int si = g_shIdx[s];
            if(si >= b)
               continue;
            if(si + InpSwingWindow > b)
               continue;
            if(b - si > InpSweepLookback + InpMaxOBCandles + InpSwingWindow)
               break;
            if(g_rates[b].high > g_shPx[s] && g_rates[b].close < g_shPx[s])
              {
               ob.hasSweep   = true;
               ob.sweepLevel = g_shPx[s];
               ob.sweepTime  = g_rates[si].time;
               for(int e = 0; e < g_shN; e++)
                  if(e != s && g_shIdx[e] < b &&
                     MathAbs(g_shPx[e] - g_shPx[s]) <= InpEqualTolerance * avg)
                    {
                     ob.sweepEqual = true;
                     break;
                    }
               break;
              }
           }
        }
     }
   if(InpRequireSweep && !ob.hasSweep)
     {
      if(InpPrintLog)
         PrintFormat("%s OB @%s رُفض: ما كنستش سيولة قبلها",
                     (bull ? "BULL" : "BEAR"), TimeToString(ob.startTime));
      return(false);
     }

   //--- 5) Mitigation / كسر الزون
   for(int b = breakIdx + 1; b < g_n; b++)
     {
      if(bull && g_rates[b].close < ob.bottom) { ob.broken = true; }
      if(!bull && g_rates[b].close > ob.top)   { ob.broken = true; }

      if(!ob.mitigated)
        {
         bool hit = false;
         if(InpMitigation == MIT_TOUCH_EDGE)
            hit = bull ? (g_rates[b].low <= ob.top) : (g_rates[b].high >= ob.bottom);
         else
            if(InpMitigation == MIT_TOUCH_CE)
               hit = bull ? (g_rates[b].low <= ob.ce) : (g_rates[b].high >= ob.ce);
            else
               hit = (g_rates[b].close <= ob.top && g_rates[b].close >= ob.bottom);
         if(hit)
           {
            ob.mitigated = true;
            ob.mitTime   = g_rates[b].time;
           }
        }
      if(ob.broken)
         break;
     }
   if(ob.broken && InpDeleteBroken)
      return(false);
   if(ob.mitigated && InpHideMitigated)
      return(false);

   //--- premium / discount
   int rFrom = (int)MathMax(0, breakIdx - (int)MathMax(10, InpRangeLookback) + 1);
   double rHigh = HighestHigh(rFrom, breakIdx);
   double rLow  = LowestLow(rFrom, breakIdx);
   if(rHigh > rLow)
      ob.rangePos = (ob.ce - rLow) / (rHigh - rLow);
   ob.inZone = bull ? (ob.rangePos <= 0.5) : (ob.rangePos >= 0.5);
   if(InpRequireDiscount && !ob.inZone)
      return(false);

   //--- OTE 62% - 79% متاع حركة الاندفاع
   double impHigh = HighestHigh(obStart, breakIdx);
   double impLow  = LowestLow(obStart, breakIdx);
   double R = impHigh - impLow;
   if(R > 0.0)
     {
      if(bull)
        {
         ob.oteTop    = impHigh - 0.62 * R;
         ob.oteBottom = impHigh - 0.79 * R;
        }
      else
        {
         ob.oteBottom = impLow + 0.62 * R;
         ob.oteTop    = impLow + 0.79 * R;
        }
      ob.hasOTE = Overlap(ob.bottom, ob.top, ob.oteBottom, ob.oteTop);
     }

   //--- التقييم
   int sc = 1;                       // كسر ستركتشر مؤكّد
   string tg = (isMSS ? "MSS" : "BOS");
   if(ob.hasSweep)  { sc++; tg += " SWEEP"; }
   if(ob.sweepEqual){ sc++; tg += "(EQ)";   }
   if(ob.hasDisp)   { sc++; tg += " DISP";  }
   if(ob.hasFVG)    { sc++; tg += " FVG";   }
   if(!ob.mitigated){ sc++; tg += " FRESH"; }
   else               tg += " MITIGATED";
   if(ob.inZone)    { sc++; tg += (bull ? " DISC" : " PREM"); }
   if(ob.hasOTE)    { sc++; tg += " OTE";   }
   ob.score = sc;
   ob.tags  = tg;
   if(sc >= 7)      ob.grade = "A+";
   else if(sc >= 6) ob.grade = "A";
   else if(sc >= 5) ob.grade = "B";
   else             ob.grade = "C";

   if(InpMinStrength == SF_B_PLUS && sc < 5)
      return(false);
   if(InpMinStrength == SF_A_ONLY && sc < 6)
      return(false);

   if(InpPrintLog)
      PrintFormat("%s OB %s @%s  [%s]  zone %.5f-%.5f  CE %.5f",
                  (bull ? "BULL" : "BEAR"), ob.grade, TimeToString(ob.startTime),
                  ob.tags, ob.bottom, ob.top, ob.ce);
   return(true);
  }

//+------------------------------------------------------------------+
//| Refinement : انزل لتايم فريم أصغر و refine داخل الزون             |
//+------------------------------------------------------------------+
void RefineAll()
  {
   ENUM_TIMEFRAMES rtf = (InpRefineTF == PERIOD_CURRENT) ? (ENUM_TIMEFRAMES)_Period : InpRefineTF;
   if(PeriodSeconds(rtf) >= PeriodSeconds(g_detTF))
      return;                                   // لازم يكون أصغر من تايم الاكتشاف

   int det = PeriodSeconds(g_detTF);
   MqlRates r[];
   ArraySetAsSeries(r, false);

   for(int i = 0; i < g_obN; i++)
     {
      datetime t1 = g_obs[i].startTime;
      datetime t2 = (datetime)(g_obs[i].endTime + det);
      int n = CopyRates(_Symbol, rtf, t1, t2, r);
      if(n < 3)
         continue;

      bool   bull = g_obs[i].bullish;
      double zTop = g_obs[i].top, zBot = g_obs[i].bottom;
      double bTop = 0.0, bBot = 0.0, bSize = 0.0;

      //--- الأفضل: FVG داخل الـ OB
      for(int m = 1; m < n - 1; m++)
        {
         double gTop = 0.0, gBot = 0.0;
         if(bull)
           {
            if(r[m + 1].low > r[m - 1].high)
              { gBot = r[m - 1].high; gTop = r[m + 1].low; }
           }
         else
           {
            if(r[m + 1].high < r[m - 1].low)
              { gTop = r[m - 1].low; gBot = r[m + 1].high; }
           }
         if(gTop <= gBot)
            continue;
         double cTop = MathMin(gTop, zTop);
         double cBot = MathMax(gBot, zBot);
         if(cTop - cBot <= 0.0)
            continue;                            // برّا الزون
         if(cTop - cBot > bSize)
           { bSize = cTop - cBot; bTop = cTop; bBot = cBot; }
        }

      //--- بديل: آخر شمعة معاكسة داخل النافذة
      if(bSize <= 0.0)
        {
         for(int m = n - 1; m >= 0; m--)
           {
            bool opp = bull ? (r[m].close < r[m].open) : (r[m].close > r[m].open);
            if(!opp)
               continue;
            double cTop = bull ? r[m].open : r[m].high;
            double cBot = bull ? r[m].low  : r[m].open;
            cTop = MathMin(cTop, zTop);
            cBot = MathMax(cBot, zBot);
            if(cTop - cBot > 0.0)
              { bSize = cTop - cBot; bTop = cTop; bBot = cBot; }
            break;
           }
        }

      if(bSize > 0.0)
        {
         g_obs[i].refined   = true;
         g_obs[i].refTop    = bTop;
         g_obs[i].refBottom = bBot;
        }
     }
  }

//+------------------------------------------------------------------+
//| Drawing                                                          |
//+------------------------------------------------------------------+
void RectObj(const string name, const datetime t1, const double p1,
             const datetime t2, const double p2, const color clr,
             const bool fill, const int width, const ENUM_LINE_STYLE style)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, p1, t2, p2);
   ObjectSetInteger(0, name, OBJPROP_TIME,  0, t1);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, p1);
   ObjectSetInteger(0, name, OBJPROP_TIME,  1, t2);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 1, p2);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FILL, fill);
   ObjectSetInteger(0, name, OBJPROP_BACK, fill);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, MathMax(1, width));
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTED,   false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
  }

void LineObj(const string name, const datetime t1, const double p1,
             const datetime t2, const double p2, const color clr,
             const int width, const ENUM_LINE_STYLE style)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2);
   ObjectSetInteger(0, name, OBJPROP_TIME,  0, t1);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, p1);
   ObjectSetInteger(0, name, OBJPROP_TIME,  1, t2);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 1, p2);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, MathMax(1, width));
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT,  false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_BACK,       false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
  }

void TextObj(const string name, const datetime t, const double p,
             const string txt, const color clr, const ENUM_ANCHOR_POINT anchor)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_TEXT, 0, t, p);
   ObjectSetInteger(0, name, OBJPROP_TIME,  0, t);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, p);
   ObjectSetString (0, name, OBJPROP_TEXT, txt);
   ObjectSetString (0, name, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, MathMax(6, InpFontSize));
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_BACK,       false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
  }

void DrawAll()
  {
   int      chartSec  = PeriodSeconds((ENUM_TIMEFRAMES)_Period);
   datetime lastTime  = (datetime)SeriesInfoInteger(_Symbol, (ENUM_TIMEFRAMES)_Period, SERIES_LASTBAR_DATE);
   datetime rightTime = (datetime)(lastTime + (long)MathMax(1, InpExtendBars) * chartSec);

   for(int i = 0; i < g_obN; i++)
     {
      string id  = g_prefix + IntegerToString(i) + "_";
      bool   bl  = g_obs[i].bullish;
      color  clr = bl ? InpBullColor : InpBearColor;
      if(g_obs[i].mitigated)
         clr = bl ? InpBullMitColor : InpBearMitColor;

      //--- الزون
      RectObj(id + "zone", g_obs[i].startTime, g_obs[i].top, rightTime, g_obs[i].bottom,
              clr, InpFillZones, 1, STYLE_SOLID);
      if(InpBorderWidth > 0)
         RectObj(id + "brd", g_obs[i].startTime, g_obs[i].top, rightTime, g_obs[i].bottom,
                 clr, false, InpBorderWidth,
                 (g_obs[i].mitigated ? STYLE_DOT : STYLE_SOLID));

      //--- CE 50%
      if(InpShowCE)
         LineObj(id + "ce", g_obs[i].startTime, g_obs[i].ce, rightTime, g_obs[i].ce,
                 InpCEColor, 1, STYLE_DASH);

      //--- FVG
      if(InpShowFVG && g_obs[i].hasFVG)
         RectObj(id + "fvg", g_obs[i].fvgT1, g_obs[i].fvgTop, rightTime, g_obs[i].fvgBottom,
                 InpFVGColor, false, 1, STYLE_DOT);

      //--- مستوى السيولة المكنوسة
      if(InpShowSweepLine && g_obs[i].hasSweep)
        {
         LineObj(id + "swp", g_obs[i].sweepTime, g_obs[i].sweepLevel,
                 g_obs[i].breakTime, g_obs[i].sweepLevel, InpSweepColor, 1, STYLE_DASHDOT);
         if(InpShowLabels)
            TextObj(id + "swpt", g_obs[i].sweepTime, g_obs[i].sweepLevel,
                    (g_obs[i].sweepEqual ? "EQ$ " : "$ "), InpSweepColor,
                    (bl ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER));
        }

      //--- OTE
      if(InpShowOTE && g_obs[i].hasOTE)
         RectObj(id + "ote", g_obs[i].startTime, g_obs[i].oteTop, rightTime, g_obs[i].oteBottom,
                 InpOTEColor, false, 1, STYLE_DOT);

      //--- الزون المكرّرة (LTF)
      if(g_obs[i].refined)
         RectObj(id + "ref", g_obs[i].startTime, g_obs[i].refTop, rightTime, g_obs[i].refBottom,
                 InpRefineColor, false, 1, STYLE_SOLID);

      //--- خط الكسر (BOS/MSS)
      LineObj(id + "brk", g_obs[i].startTime, g_obs[i].breakLevel,
              g_obs[i].breakTime, g_obs[i].breakLevel, clr, 1, STYLE_DOT);

      //--- التسمية
      if(InpShowLabels)
        {
         string txt = StringFormat("%s OB %s | %s",
                                   (bl ? "BULL" : "BEAR"), g_obs[i].grade, g_obs[i].tags);
         TextObj(id + "lbl", g_obs[i].startTime, (bl ? g_obs[i].top : g_obs[i].bottom), txt,
                 InpTextColor, (bl ? ANCHOR_LEFT_LOWER : ANCHOR_LEFT_UPPER));
        }
     }
  }

//+------------------------------------------------------------------+
//| Alerts                                                           |
//+------------------------------------------------------------------+
bool AlertedBefore(const datetime &arr[], const datetime t)
  {
   for(int i = ArraySize(arr) - 1; i >= 0; i--)
      if(arr[i] == t)
         return(true);
   return(false);
  }

void MarkAlerted(datetime &arr[], const datetime t)
  {
   int n = ArraySize(arr);
   if(n >= 300)
     {
      for(int i = 0; i < n - 1; i++)
         arr[i] = arr[i + 1];
      arr[n - 1] = t;
      return;
     }
   ArrayResize(arr, n + 1);
   arr[n] = t;
  }

void FireAlert(const string msg)
  {
   if(InpAlertPopup)
      Alert(msg);
   if(InpAlertPush)
      SendNotification(msg);
   if(InpAlertMail)
      SendMail("SMC OrderBlock", msg);
   if(!InpAlertPopup && !InpAlertPush && !InpAlertMail)
      Print(msg);
  }

void CheckFormAlerts()
  {
   if(g_obN <= 0)
      return;
   int i = g_obN - 1;
   if(g_obs[i].breakIdx < g_n - 3)         // برك الـ OB الطازجة
      return;
   if(AlertedBefore(g_alertForm, g_obs[i].startTime))
      return;
   MarkAlerted(g_alertForm, g_obs[i].startTime);
   FireAlert(StringFormat("%s %s: %s OB %s جديدة | %s | zone %s-%s | CE %s",
                          _Symbol, TFName(g_detTF),
                          (g_obs[i].bullish ? "BULL" : "BEAR"), g_obs[i].grade, g_obs[i].tags,
                          DoubleToString(g_obs[i].bottom, _Digits),
                          DoubleToString(g_obs[i].top, _Digits),
                          DoubleToString(g_obs[i].ce, _Digits)));
  }

void CheckTapAlerts()
  {
   if(!InpAlertTap || g_obN <= 0)
      return;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid <= 0.0)
      return;
   for(int i = 0; i < g_obN; i++)
     {
      if(g_obs[i].mitigated)
         continue;
      if(bid > g_obs[i].top || bid < g_obs[i].bottom)
         continue;
      if(AlertedBefore(g_alertTap, g_obs[i].startTime))
         continue;
      MarkAlerted(g_alertTap, g_obs[i].startTime);
      FireAlert(StringFormat("%s %s: السعر دخل %s OB %s | CE %s",
                             _Symbol, TFName(g_detTF),
                             (g_obs[i].bullish ? "BULL" : "BEAR"), g_obs[i].grade,
                             DoubleToString(g_obs[i].ce, _Digits)));
     }
  }
//+------------------------------------------------------------------+

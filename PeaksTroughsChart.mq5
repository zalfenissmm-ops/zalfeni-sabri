//+------------------------------------------------------------------+
//| PeaksTroughsChart.mq5                                            |
//| Fractal peaks/troughs with delayed confirmation, cancellation,   |
//| zigzag, main range (built only from confirmed range breaks) and  |
//| trend (HH/HL vs LH/LL) — drawn directly on the chart.             |
//|                                                                    |
//| Rules (priority order):                                           |
//|  1. Choose N (3, 5 or 7) = window size.                           |
//|  2. Middle candle of the window is the candidate                  |
//|     (peak = max high in window, trough = min low in window).     |
//|  3. Confirmation lags (N-1)/2 candles after the middle candle.    |
//|  4. Cancellation: two same-type points in a row with no opposite  |
//|     point between them -> keep the stronger one, drop the other. |
//|  5. Zigzag: link every confirmed point to the previous one ->     |
//|     strictly alternating peak/trough/peak/trough.                 |
//|  6. Main range: last two consecutive zigzag points fix two flat   |
//|     horizontal boundaries.                                        |
//|  7. A move that stays inside the range is secondary.              |
//|  8. A range break creates a new main point (break above =         |
//|     trend continuation, break below = potential reversal) -> a    |
//|     new range is drawn from the new point + the point before it. |
//|  9. Trend: consecutive main HH+HL = uptrend, LH+LL = downtrend.   |
//+------------------------------------------------------------------+
#property copyright "peaks-troughs-chart"
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

input int   InpWindow               = 5;     // نافذة الفراكتال N (لازم 3 أو 5 أو 7) - قاعدة 1
input int   InpLookbackBars         = 400;    // عدد الشموع المحلّلة للخلف
input bool  InpShowSecondaryZigzag  = true;   // إظهار الزجزاج الفرعي (رمادي منقّط)
input bool  InpShowHistoricalRanges = true;   // إظهار مربعات الرينج القديمة
input int   InpMaxHistoricalRanges  = 6;      // أقصى عدد مربعات رينج قديمة تُعرض
input int   InpFutureExtensionBars  = 20;     // كم شمعة يمتد الرينج النشط لليمين
input color InpMainColor            = clrOrange;
input color InpZigzagColor          = clrSilver;
input color InpActiveRangeColor     = clrDodgerBlue;
input color InpOldRangeColor        = clrGray;

#define PFX "PTC_"

//--- one confirmed / candidate swing point
struct SwingPoint
  {
   int      bar;      // local bar index (0 = oldest in the lookback window)
   datetime time;
   double   price;
   string   kind;      // "peak" | "trough"
  };

//--- a raw fractal candidate before the cancellation rule
struct Candidate
  {
   int    bar;
   int    confirmed;
   string kind;
   double price;
  };

//--- one main-range segment (rule 6-8)
struct Segment
  {
   SwingPoint upper;
   SwingPoint lower;
   int        endBar;
   bool       hasEnd;
   string     brk;      // "" | "up" | "down"
  };

datetime g_lastBarTime = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpWindow != 3 && InpWindow != 5 && InpWindow != 7)
      Print("PeaksTroughsChart: InpWindow يجب أن يكون 3 أو 5 أو 7 (قاعدة 1) — استُخدم 5 بدلاً منه.");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, PFX);
  }

//+------------------------------------------------------------------+
//| Rule 1-3: fractal candidates with their confirmation lag          |
//+------------------------------------------------------------------+
void FindCandidates(const double &h[], const double &l[], int count, int n, Candidate &out[])
  {
   ArrayResize(out, 0);
   int half = (n - 1) / 2;
   if(count < n)
      return;

   for(int i = half; i < count - half; i++)
     {
      double hmax = h[i - half];
      double lmin = l[i - half];
      for(int j = i - half; j <= i + half; j++)
        {
         if(h[j] > hmax) hmax = h[j];
         if(l[j] < lmin) lmin = l[j];
        }
      if(h[i] == hmax)
        {
         int c = ArraySize(out);
         ArrayResize(out, c + 1);
         out[c].bar = i; out[c].confirmed = i + half; out[c].kind = "peak"; out[c].price = h[i];
        }
      if(l[i] == lmin)
        {
         int c = ArraySize(out);
         ArrayResize(out, c + 1);
         out[c].bar = i; out[c].confirmed = i + half; out[c].kind = "trough"; out[c].price = l[i];
        }
     }
  }

//+------------------------------------------------------------------+
//| Rule 4-5: cancel same-type runs down to the strongest point ->    |
//| leaves a strictly alternating peak/trough/peak/trough zigzag      |
//+------------------------------------------------------------------+
void ResolveZigzag(Candidate &cands[], const datetime &t[], SwingPoint &zz[])
  {
   ArrayResize(zz, 0);
   int n = ArraySize(cands);
   int top = -1;
   for(int i = 0; i < n; i++)
     {
      if(top >= 0 && zz[top].kind == cands[i].kind)
        {
         bool stronger = (cands[i].kind == "peak") ? (cands[i].price > zz[top].price)
                                                     : (cands[i].price < zz[top].price);
         if(stronger)
           {
            zz[top].bar   = cands[i].bar;
            zz[top].time  = t[cands[i].bar];
            zz[top].price = cands[i].price;
           }
        }
      else
        {
         top++;
         ArrayResize(zz, top + 1);
         zz[top].bar   = cands[i].bar;
         zz[top].time  = t[cands[i].bar];
         zz[top].price = cands[i].price;
         zz[top].kind  = cands[i].kind;
        }
     }
  }

//+------------------------------------------------------------------+
//| Rule 6-8: track the active main range, grow it only on a break    |
//+------------------------------------------------------------------+
void BuildMainStructure(SwingPoint &zz[], SwingPoint &mainPts[], SwingPoint &mainHighs[],
                         SwingPoint &mainLows[], Segment &segs[])
  {
   ArrayResize(mainPts, 0);
   ArrayResize(mainHighs, 0);
   ArrayResize(mainLows, 0);
   ArrayResize(segs, 0);

   int n = ArraySize(zz);
   if(n < 2)
      return;

   SwingPoint p0 = zz[0], p1 = zz[1];
   SwingPoint upper, lower;
   if(p0.kind == "peak") { upper = p0; lower = p1; }
   else                  { upper = p1; lower = p0; }

   ArrayResize(mainHighs, 1); mainHighs[0] = upper;
   ArrayResize(mainLows, 1);  mainLows[0]  = lower;
   ArrayResize(mainPts, 2);   mainPts[0] = p0; mainPts[1] = p1;

   ArrayResize(segs, 1);
   segs[0].upper = upper; segs[0].lower = lower; segs[0].hasEnd = false; segs[0].brk = "";

   for(int k = 2; k < n; k++)
     {
      SwingPoint prevPt = zz[k - 1];
      SwingPoint curPt  = zz[k];
      string broke = "";
      if(curPt.kind == "peak" && curPt.price > upper.price)
         broke = "up";
      else if(curPt.kind == "trough" && curPt.price < lower.price)
         broke = "down";

      if(broke == "")
         continue; // inside the range -> secondary move (rule 7)

      int last = ArraySize(segs) - 1;
      segs[last].endBar = curPt.bar;
      segs[last].hasEnd = true;

      if(broke == "up") { upper = curPt; lower = prevPt; }
      else               { lower = curPt; upper = prevPt; }

      int mp = ArraySize(mainPts); ArrayResize(mainPts, mp + 1); mainPts[mp] = curPt;
      if(curPt.kind == "peak")
        { int mh = ArraySize(mainHighs); ArrayResize(mainHighs, mh + 1); mainHighs[mh] = curPt; }
      else
        { int ml = ArraySize(mainLows); ArrayResize(mainLows, ml + 1); mainLows[ml] = curPt; }

      int ns = ArraySize(segs); ArrayResize(segs, ns + 1);
      segs[ns].upper = upper; segs[ns].lower = lower; segs[ns].hasEnd = false; segs[ns].brk = broke;
     }
  }

//+------------------------------------------------------------------+
//| Rule 9: consecutive main HH+HL -> uptrend, LH+LL -> downtrend     |
//+------------------------------------------------------------------+
string DetermineTrend(SwingPoint &mh[], SwingPoint &ml[])
  {
   int nh = ArraySize(mh), nl = ArraySize(ml);
   if(nh < 2 || nl < 2)
      return "Unclear";
   bool higherHigh = mh[nh - 1].price > mh[nh - 2].price;
   bool higherLow  = ml[nl - 1].price > ml[nl - 2].price;
   bool lowerHigh  = mh[nh - 1].price < mh[nh - 2].price;
   bool lowerLow   = ml[nl - 1].price < ml[nl - 2].price;
   if(higherHigh && higherLow) return "Uptrend";
   if(lowerHigh && lowerLow)   return "Downtrend";
   return "Mixed";
  }

//+------------------------------------------------------------------+
string PointLabel(SwingPoint &sp, SwingPoint &mh[], SwingPoint &ml[])
  {
   if(sp.kind == "peak")
     {
      for(int j = 0; j < ArraySize(mh); j++)
         if(mh[j].bar == sp.bar)
            return (j == 0) ? "H" : ((mh[j].price > mh[j - 1].price) ? "HH" : "LH");
     }
   else
     {
      for(int j = 0; j < ArraySize(ml); j++)
         if(ml[j].bar == sp.bar)
            return (j == 0) ? "L" : ((ml[j].price > ml[j - 1].price) ? "HL" : "LL");
     }
   return "";
  }

//+------------------------------------------------------------------+
void DrawSegmentLine(string name, datetime t1, double p1, datetime t2, double p2,
                      color clr, int width, ENUM_LINE_STYLE style)
  {
   ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
  }

//+------------------------------------------------------------------+
void DrawMarker(string name, datetime t, double p, color clr, int size, int code)
  {
   ObjectCreate(0, name, OBJ_ARROW, 0, t, p);
   ObjectSetInteger(0, name, OBJPROP_ARROWCODE, code);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, size);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void DrawLabel(string name, datetime t, double p, string text, color clr, int fontSize, bool above)
  {
   ObjectCreate(0, name, OBJ_TEXT, 0, t, p);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fontSize);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, above ? ANCHOR_LOWER : ANCHOR_UPPER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void DrawRangeBox(string name, datetime t1, datetime t2, double upper, double lower,
                   color clr, bool active)
  {
   ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, upper, t2, lower);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FILL, active);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_STYLE, active ? STYLE_SOLID : STYLE_DOT);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, active ? 2 : 1);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void RenderInfoLabel(string text, string trend)
  {
   string name = PFX + "info";
   ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 10);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 20);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 10);
   color c = clrSilver;
   if(trend == "Uptrend")   c = clrLime;
   if(trend == "Downtrend") c = clrRed;
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
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
   if(rates_total < 3)
      return(rates_total);

   // recompute only when a new bar has closed (fractal confirmation needs closed bars anyway)
   if(time[rates_total - 1] == g_lastBarTime)
      return(rates_total);
   g_lastBarTime = time[rates_total - 1];

   int n = InpWindow;
   if(n != 3 && n != 5 && n != 7)
      n = 5;

   int start = rates_total - InpLookbackBars;
   if(start < 0) start = 0;
   int count = rates_total - start;
   if(count < n)
      return(rates_total);

   double h[], l[];
   datetime t[];
   ArrayResize(h, count);
   ArrayResize(l, count);
   ArrayResize(t, count);
   for(int k = 0; k < count; k++)
     {
      h[k] = high[start + k];
      l[k] = low[start + k];
      t[k] = time[start + k];
     }

   Candidate cands[];
   FindCandidates(h, l, count, n, cands);

   SwingPoint zz[];
   ResolveZigzag(cands, t, zz);

   ObjectsDeleteAll(0, PFX);

   if(ArraySize(zz) < 2)
     {
      RenderInfoLabel(StringFormat("Peaks & Troughs N=%d\nلم يتكوّن عدد كافٍ من النقاط بعد", n), "Unclear");
      return(rates_total);
     }

   SwingPoint mainPts[], mainHighs[], mainLows[];
   Segment segs[];
   BuildMainStructure(zz, mainPts, mainHighs, mainLows, segs);

   string trend = DetermineTrend(mainHighs, mainLows);

   // secondary zigzag (rule 5)
   if(InpShowSecondaryZigzag)
     {
      for(int i = 0; i < ArraySize(zz) - 1; i++)
         DrawSegmentLine(PFX + "zzl_" + IntegerToString(i), zz[i].time, zz[i].price,
                          zz[i + 1].time, zz[i + 1].price, InpZigzagColor, 1, STYLE_DOT);
      for(int i = 0; i < ArraySize(zz); i++)
         DrawMarker(PFX + "zzp_" + IntegerToString(i), zz[i].time, zz[i].price, InpZigzagColor, 1, 159);
     }

   // main structure line + labels (rule 6-9)
   for(int i = 0; i < ArraySize(mainPts) - 1; i++)
      DrawSegmentLine(PFX + "ml_" + IntegerToString(i), mainPts[i].time, mainPts[i].price,
                       mainPts[i + 1].time, mainPts[i + 1].price, InpMainColor, 2, STYLE_SOLID);
   for(int i = 0; i < ArraySize(mainPts); i++)
     {
      DrawMarker(PFX + "mp_" + IntegerToString(i), mainPts[i].time, mainPts[i].price, InpMainColor, 3, 159);
      string lbl = PointLabel(mainPts[i], mainHighs, mainLows);
      bool above = (mainPts[i].kind == "peak");
      DrawLabel(PFX + "mlbl_" + IntegerToString(i), mainPts[i].time, mainPts[i].price, lbl,
                InpMainColor, 8, above);
     }

   // range boxes (rule 6-8): historical ones dim/dashed, the active (last) one solid & highlighted
   int nseg = ArraySize(segs);
   int firstSeg = nseg - 1;
   if(InpShowHistoricalRanges)
     {
      firstSeg = nseg - 1 - InpMaxHistoricalRanges;
      if(firstSeg < 0) firstSeg = 0;
     }
   datetime futureTime = t[count - 1] + PeriodSeconds() * InpFutureExtensionBars;
   for(int si = firstSeg; si < nseg; si++)
     {
      bool active = (si == nseg - 1);
      datetime xStart = (segs[si].upper.time < segs[si].lower.time) ? segs[si].upper.time : segs[si].lower.time;
      datetime xEnd   = segs[si].hasEnd ? t[segs[si].endBar] : futureTime;
      DrawRangeBox(PFX + "seg_" + IntegerToString(si), xStart, xEnd,
                   segs[si].upper.price, segs[si].lower.price,
                   active ? InpActiveRangeColor : InpOldRangeColor, active);
     }

   SwingPoint up = segs[nseg - 1].upper;
   SwingPoint lo = segs[nseg - 1].lower;
   string info = StringFormat("Peaks & Troughs N=%d\nTrend: %s\nRange upper: %s\nRange lower: %s",
                               n, trend, DoubleToString(up.price, _Digits), DoubleToString(lo.price, _Digits));
   RenderInfoLabel(info, trend);

   ChartRedraw(0);
   return(rates_total);
  }
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//|                                              ZalfeniSignals.mq5  |
//|  Buy/sell signals drawn straight on the chart, built from the    |
//|  same three confirming reads as trend_identifier.py:             |
//|    1. price position relative to an EMA                          |
//|    2. swing structure (Higher-High/Higher-Low vs Lower/Lower)    |
//|    3. ADX, to tell a real trend from a ranging market            |
//|                                                                  |
//|  A signal prints only when the combined bias FLIPS and ADX is    |
//|  above the strength floor, so you get one arrow per turn, not    |
//|  an arrow on every bar.                                          |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property link      "https://github.com/zalfenissmm-ops/zalfeni-sabri"
#property version   "1.00"
#property description "Buy/sell arrows from three confirming reads: price vs EMA, swing structure (HH/HL vs LH/LL) and ADX strength."
#property description "Arrows print on closed bars only (no repaint), with popup/push/sound alerts and a suggested SL/TP."

#property indicator_chart_window
#property indicator_buffers 11
#property indicator_plots   3

//--- plot 1: buy arrow
#property indicator_label1  "Buy"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrDodgerBlue
#property indicator_width1  2
//--- plot 2: sell arrow
#property indicator_label2  "Sell"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrOrangeRed
#property indicator_width2  2
//--- plot 3: trend EMA
#property indicator_label3  "EMA"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrGoldenrod
#property indicator_style3  STYLE_DOT
#property indicator_width3  1

//--- trend engine ---------------------------------------------------
input int    InpEmaPeriod      = 50;      // EMA period (price position)
input int    InpAdxPeriod      = 14;      // ADX period (trend strength)
input double InpAdxMinStrength = 20.0;    // Minimum ADX to allow a signal
input int    InpSwingWindow    = 5;       // Swing window (bars on each side)
input bool   InpResetOnNeutral = false;   // Re-arm the same direction after the trend goes neutral
input bool   InpClosedBarsOnly = true;    // Signal on closed bars only (no repaint)
//--- levels ---------------------------------------------------------
input double InpAtrArrowGap    = 0.6;     // Arrow distance from the bar (x ATR)
input double InpAtrStopPad     = 0.5;     // Stop-loss padding beyond the swing (x ATR)
input double InpRiskReward     = 2.0;     // Take-profit = R:R x risk
input bool   InpShowLevelLines = true;    // Draw entry/SL/TP lines of the last signal
//--- display --------------------------------------------------------
input bool   InpShowEma        = true;    // Show the EMA line
input bool   InpShowPanel      = true;    // Show the info panel
input int    InpPanelX         = 12;      // Panel X offset (pixels)
input int    InpPanelY         = 18;      // Panel Y offset (pixels)
//--- alerts ---------------------------------------------------------
input bool   InpAlertPopup     = true;    // Popup alert on a new signal
input bool   InpAlertPush      = false;   // Push notification to the mobile terminal
input bool   InpAlertSound     = false;   // Play a sound on a new signal
input string InpSoundFile      = "alert.wav"; // Sound file

//--- plotted buffers
double g_buy[];
double g_sell[];
double g_ema[];
//--- working buffers
double g_adx[];
double g_atr[];
double g_h1[];      // last confirmed swing high as of this bar
double g_h2[];      // the one before it
double g_l1[];      // last confirmed swing low as of this bar
double g_l2[];      // the one before it
double g_bias[];    // +1 up / -1 down / 0 unclear
double g_state[];   // direction of the last printed arrow (+1 / -1 / 0)

//--- indicator handles
int g_emaHandle = INVALID_HANDLE;
int g_adxHandle = INVALID_HANDLE;
int g_atrHandle = INVALID_HANDLE;

//--- first bar we can evaluate
int      g_first = 0;
string   g_prefix = "ZS_";
datetime g_lastAlertBar = 0;

#define PANEL_ROWS 7

//+------------------------------------------------------------------+
//| Initialisation                                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   if(InpEmaPeriod < 2 || InpAdxPeriod < 2 || InpSwingWindow < 1)
   {
      Print("ZalfeniSignals: EMA period and ADX period must be >= 2, swing window >= 1.");
      return(INIT_PARAMETERS_INCORRECT);
   }
   if(InpRiskReward <= 0.0)
   {
      Print("ZalfeniSignals: risk/reward must be greater than 0.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   SetIndexBuffer(0, g_buy,   INDICATOR_DATA);
   SetIndexBuffer(1, g_sell,  INDICATOR_DATA);
   SetIndexBuffer(2, g_ema,   INDICATOR_DATA);
   SetIndexBuffer(3, g_adx,   INDICATOR_CALCULATIONS);
   SetIndexBuffer(4, g_atr,   INDICATOR_CALCULATIONS);
   SetIndexBuffer(5, g_h1,    INDICATOR_CALCULATIONS);
   SetIndexBuffer(6, g_h2,    INDICATOR_CALCULATIONS);
   SetIndexBuffer(7, g_l1,    INDICATOR_CALCULATIONS);
   SetIndexBuffer(8, g_l2,    INDICATOR_CALCULATIONS);
   SetIndexBuffer(9, g_bias,  INDICATOR_CALCULATIONS);
   SetIndexBuffer(10, g_state, INDICATOR_CALCULATIONS);

   PlotIndexSetInteger(0, PLOT_ARROW, 233);   // up arrow
   PlotIndexSetInteger(1, PLOT_ARROW, 234);   // down arrow
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(2, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetInteger(2, PLOT_DRAW_TYPE, InpShowEma ? DRAW_LINE : DRAW_NONE);
   PlotIndexSetString(2, PLOT_LABEL, "EMA" + IntegerToString(InpEmaPeriod));

   g_emaHandle = iMA(_Symbol, PERIOD_CURRENT, InpEmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   g_adxHandle = iADX(_Symbol, PERIOD_CURRENT, InpAdxPeriod);
   g_atrHandle = iATR(_Symbol, PERIOD_CURRENT, 14);
   if(g_emaHandle == INVALID_HANDLE || g_adxHandle == INVALID_HANDLE || g_atrHandle == INVALID_HANDLE)
   {
      Print("ZalfeniSignals: failed to create the EMA/ADX/ATR handles, error ", GetLastError());
      return(INIT_FAILED);
   }

   // ADX needs 2x its period to settle, a swing needs a full window on each side.
   g_first = (int)MathMax(InpEmaPeriod, InpAdxPeriod * 2) + 2 * InpSwingWindow + 2;

   PlotIndexSetInteger(0, PLOT_DRAW_BEGIN, g_first);
   PlotIndexSetInteger(1, PLOT_DRAW_BEGIN, g_first);
   PlotIndexSetInteger(2, PLOT_DRAW_BEGIN, g_first);

   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   IndicatorSetString(INDICATOR_SHORTNAME,
                      StringFormat("Zalfeni Signals (EMA%d, ADX%d>%.0f, swing %d)",
                                   InpEmaPeriod, InpAdxPeriod, InpAdxMinStrength, InpSwingWindow));

   g_prefix = "ZS_" + IntegerToString(ChartID()) + "_";
   g_lastAlertBar = 0;
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Clean up the chart objects we own                                |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, g_prefix);
   ChartRedraw();
   if(g_emaHandle != INVALID_HANDLE) IndicatorRelease(g_emaHandle);
   if(g_adxHandle != INVALID_HANDLE) IndicatorRelease(g_adxHandle);
   if(g_atrHandle != INVALID_HANDLE) IndicatorRelease(g_atrHandle);
}

//+------------------------------------------------------------------+
//| Fractal swing points: the bar is the extreme of its window        |
//| (ties allowed, same rule as trend_identifier.py)                  |
//+------------------------------------------------------------------+
bool IsSwingHigh(const double &high[], const int pivot, const int window)
{
   const double level = high[pivot];
   for(int k = pivot - window; k <= pivot + window; k++)
      if(high[k] > level) return(false);
   return(true);
}

bool IsSwingLow(const double &low[], const int pivot, const int window)
{
   const double level = low[pivot];
   for(int k = pivot - window; k <= pivot + window; k++)
      if(low[k] < level) return(false);
   return(true);
}

//+------------------------------------------------------------------+
//| Main calculation                                                 |
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
   if(rates_total < g_first + 2) return(0);

   // Wait until the sub-indicators have covered the whole history.
   if(BarsCalculated(g_emaHandle) < rates_total ||
      BarsCalculated(g_adxHandle) < rates_total ||
      BarsCalculated(g_atrHandle) < rates_total)
      return(0);

   int start = (prev_calculated > 0) ? prev_calculated - 1 : 0;
   if(start > rates_total - 1) start = rates_total - 1;
   const int count = rates_total - start;

   double tmp[];
   if(CopyBuffer(g_emaHandle, 0, 0, count, tmp) < count) return(0);
   for(int i = 0; i < count; i++) g_ema[start + i] = tmp[i];
   if(CopyBuffer(g_adxHandle, MAIN_LINE, 0, count, tmp) < count) return(0);
   for(int i = 0; i < count; i++) g_adx[start + i] = tmp[i];
   if(CopyBuffer(g_atrHandle, 0, 0, count, tmp) < count) return(0);
   for(int i = 0; i < count; i++) g_atr[start + i] = tmp[i];

   for(int i = start; i < rates_total; i++)
   {
      g_buy[i]  = EMPTY_VALUE;
      g_sell[i] = EMPTY_VALUE;

      if(i < g_first)
      {
         g_h1[i] = EMPTY_VALUE; g_h2[i] = EMPTY_VALUE;
         g_l1[i] = EMPTY_VALUE; g_l2[i] = EMPTY_VALUE;
         g_bias[i] = 0.0;
         g_state[i] = 0.0;
         continue;
      }

      // Carry the confirmed structure forward from the previous bar.
      double h1 = g_h1[i-1], h2 = g_h2[i-1];
      double l1 = g_l1[i-1], l2 = g_l2[i-1];
      double state = g_state[i-1];

      // On the still-forming bar nothing is confirmed: keep the previous
      // state untouched so no arrow can appear and then vanish.
      const bool forming = (i == rates_total - 1) && InpClosedBarsOnly;

      if(!forming)
      {
         // The pivot InpSwingWindow bars back becomes confirmed on this bar.
         const int pivot = i - InpSwingWindow;
         if(IsSwingHigh(high, pivot, InpSwingWindow)) { h2 = h1; h1 = high[pivot]; }
         if(IsSwingLow(low, pivot, InpSwingWindow))   { l2 = l1; l1 = low[pivot];  }
      }

      g_h1[i] = h1; g_h2[i] = h2;
      g_l1[i] = l1; g_l2[i] = l2;

      // Signal 1 - swing structure: HH+HL = up, LH+LL = down, anything else mixed.
      int structure = 0;
      if(h1 != EMPTY_VALUE && h2 != EMPTY_VALUE && l1 != EMPTY_VALUE && l2 != EMPTY_VALUE)
      {
         const bool higher_high = (h1 > h2);
         const bool higher_low  = (l1 > l2);
         if(higher_high && higher_low)        structure = 1;
         else if(!higher_high && !higher_low) structure = -1;
      }

      // Signal 2 - price against the EMA.
      const int price_vs_ema = (close[i] > g_ema[i]) ? 1 : -1;

      // Combined bias: the two directional reads vote, no single one decides.
      const int votes_up   = ((price_vs_ema == 1) ? 1 : 0) + ((structure == 1) ? 1 : 0);
      const int votes_down = ((price_vs_ema == -1) ? 1 : 0) + ((structure == -1) ? 1 : 0);
      int bias = 0;
      if(votes_up > votes_down)      bias = 1;
      else if(votes_down > votes_up) bias = -1;
      g_bias[i] = (double)bias;

      // Signal 3 - ADX says whether the trend is worth trading at all.
      const bool strong = (g_adx[i] >= InpAdxMinStrength);

      if(!forming)
      {
         const double gap = InpAtrArrowGap * g_atr[i];

         if(InpResetOnNeutral && bias == 0)
            state = 0.0;

         if(strong && bias == 1 && state != 1.0)
         {
            g_buy[i] = low[i] - gap;
            state = 1.0;
         }
         else if(strong && bias == -1 && state != -1.0)
         {
            g_sell[i] = high[i] + gap;
            state = -1.0;
         }
      }

      g_state[i] = state;
   }

   const int last = rates_total - 1;
   NotifyAndDraw(rates_total, prev_calculated, time, high, low, close, last);

   return(rates_total);
}

//+------------------------------------------------------------------+
//| Suggested stop-loss / take-profit for a signal on bar `bar`      |
//+------------------------------------------------------------------+
void SignalLevels(const int direction, const int bar, const double &high[], const double &low[],
                  const double &close[], double &entry, double &sl, double &tp)
{
   entry = close[bar];
   const double pad = InpAtrStopPad * g_atr[bar];

   if(direction > 0)
   {
      double swing = (g_l1[bar] != EMPTY_VALUE) ? g_l1[bar] : low[bar];
      sl = swing - pad;
      if(sl >= entry) sl = entry - pad;          // never put the stop above the entry
      tp = entry + InpRiskReward * (entry - sl);
   }
   else
   {
      double swing = (g_h1[bar] != EMPTY_VALUE) ? g_h1[bar] : high[bar];
      sl = swing + pad;
      if(sl <= entry) sl = entry + pad;
      tp = entry - InpRiskReward * (sl - entry);
   }
}

//+------------------------------------------------------------------+
//| Alerts, level lines and the info panel                           |
//+------------------------------------------------------------------+
void NotifyAndDraw(const int rates_total, const int prev_calculated, const datetime &time[],
                   const double &high[], const double &low[], const double &close[], const int last)
{
   // Most recent signal, looking back a bounded number of bars.
   int    sigBar = -1;
   int    sigDir = 0;
   const int lookback = (int)MathMin(rates_total - g_first, 2000);
   for(int i = last; i > last - lookback && i >= g_first; i--)
   {
      if(g_buy[i] != EMPTY_VALUE)  { sigBar = i; sigDir = 1;  break; }
      if(g_sell[i] != EMPTY_VALUE) { sigBar = i; sigDir = -1; break; }
   }

   double entry = 0.0, sl = 0.0, tp = 0.0;
   if(sigBar >= 0)
      SignalLevels(sigDir, sigBar, high, low, close, entry, sl, tp);

   // Alert once per signal bar, and never while replaying history on load.
   if(sigBar >= 0 && prev_calculated > 0 && time[sigBar] != g_lastAlertBar)
   {
      if(g_lastAlertBar != 0)
      {
         const string dirText = (sigDir > 0) ? "BUY" : "SELL";
         const string msg = StringFormat("%s %s: %s signal @ %s | SL %s | TP %s | ADX %.1f",
                                         _Symbol, TfString(), dirText,
                                         DoubleToString(entry, _Digits),
                                         DoubleToString(sl, _Digits),
                                         DoubleToString(tp, _Digits),
                                         g_adx[sigBar]);
         if(InpAlertPopup) Alert(msg);
         if(InpAlertPush)  SendNotification(msg);
         if(InpAlertSound) PlaySound(InpSoundFile);
      }
      g_lastAlertBar = time[sigBar];
   }

   if(InpShowLevelLines && sigBar >= 0)
   {
      SetLine("entry", entry, clrSilver,     STYLE_DOT);
      SetLine("sl",    sl,    clrOrangeRed,  STYLE_DASH);
      SetLine("tp",    tp,    clrMediumSeaGreen, STYLE_DASH);
   }
   else
   {
      ObjectDelete(0, g_prefix + "entry");
      ObjectDelete(0, g_prefix + "sl");
      ObjectDelete(0, g_prefix + "tp");
   }

   if(InpShowPanel)
      DrawPanel(last, sigBar, sigDir, time, close, entry, sl, tp);
   else
   {
      ObjectsDeleteAll(0, g_prefix + "row");
      ObjectDelete(0, g_prefix + "bg");
   }

   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Info panel                                                       |
//+------------------------------------------------------------------+
void DrawPanel(const int last, const int sigBar, const int sigDir, const datetime &time[],
               const double &close[], const double entry, const double sl, const double tp)
{
   const string bg = g_prefix + "bg";
   if(ObjectFind(0, bg) < 0)
   {
      ObjectCreate(0, bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, bg, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, C'22,26,34');
      ObjectSetInteger(0, bg, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, bg, OBJPROP_COLOR, C'60,68,82');
      ObjectSetInteger(0, bg, OBJPROP_BACK, false);
      ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, InpPanelX - 6);
   ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, InpPanelY - 6);
   ObjectSetInteger(0, bg, OBJPROP_XSIZE, 250);
   ObjectSetInteger(0, bg, OBJPROP_YSIZE, PANEL_ROWS * 16 + 12);

   const int    bias      = (int)g_bias[last];
   const double adx       = g_adx[last];
   const bool   above_ema = (close[last] > g_ema[last]);

   string structure = "unknown";
   if(g_h1[last] != EMPTY_VALUE && g_h2[last] != EMPTY_VALUE &&
      g_l1[last] != EMPTY_VALUE && g_l2[last] != EMPTY_VALUE)
   {
      const bool hh = (g_h1[last] > g_h2[last]);
      const bool hl = (g_l1[last] > g_l2[last]);
      if(hh && hl)        structure = "HH / HL (up)";
      else if(!hh && !hl) structure = "LH / LL (down)";
      else                structure = "mixed";
   }

   string dirText = "SIDEWAYS / UNCLEAR";
   color  dirColor = clrGoldenrod;
   if(bias == 1)       { dirText = "UPTREND";   dirColor = clrDodgerBlue; }
   else if(bias == -1) { dirText = "DOWNTREND"; dirColor = clrOrangeRed;  }

   SetRow(0, StringFormat("ZALFENI SIGNALS   %s %s", _Symbol, TfString()), clrWhite, 10);
   SetRow(1, "Trend     : " + dirText, dirColor, 9);
   SetRow(2, StringFormat("Strength  : %s (ADX %.1f)", StrengthText(adx), adx),
          (adx >= InpAdxMinStrength) ? clrLightGray : clrGray, 9);
   SetRow(3, StringFormat("Price/EMA%d: %s", InpEmaPeriod, above_ema ? "above" : "below"),
          clrLightGray, 9);
   SetRow(4, "Structure : " + structure, clrLightGray, 9);

   if(sigBar >= 0)
   {
      SetRow(5, StringFormat("Signal    : %s  %s", (sigDir > 0) ? "BUY" : "SELL",
                             TimeToString(time[sigBar], TIME_DATE | TIME_MINUTES)),
             (sigDir > 0) ? clrDodgerBlue : clrOrangeRed, 9);
      SetRow(6, StringFormat("E/SL/TP   : %s / %s / %s",
                             DoubleToString(entry, _Digits),
                             DoubleToString(sl, _Digits),
                             DoubleToString(tp, _Digits)), clrLightGray, 9);
   }
   else
   {
      SetRow(5, "Signal    : none yet", clrGray, 9);
      SetRow(6, "E/SL/TP   : -", clrGray, 9);
   }
}

//+------------------------------------------------------------------+
//| One panel line                                                   |
//+------------------------------------------------------------------+
void SetRow(const int row, const string text, const color clr, const int fontsize)
{
   const string name = g_prefix + "row" + IntegerToString(row);
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
      ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   }
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, InpPanelX);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, InpPanelY + row * 16);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fontsize);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
}

//+------------------------------------------------------------------+
//| One horizontal level line                                        |
//+------------------------------------------------------------------+
void SetLine(const string suffix, const double price, const color clr, const int style)
{
   const string name = g_prefix + suffix;
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   }
   ObjectSetDouble(0, name, OBJPROP_PRICE, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, suffix + " " + DoubleToString(price, _Digits));
}

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
string StrengthText(const double adx)
{
   if(adx < 20.0) return("weak / ranging");
   if(adx < 25.0) return("developing");
   return("strong");
}

string TfString()
{
   return(StringSubstr(EnumToString((ENUM_TIMEFRAMES)_Period), 7));
}
//+------------------------------------------------------------------+

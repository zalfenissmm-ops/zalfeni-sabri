//+------------------------------------------------------------------+
//|                                             Draw_OB_Levels.mq5   |
//|   Script: draws given order block / FVG zones on the open chart  |
//|   سكريبت: يرسم مناطق الأوردر بلوك والفجوات على الشارت المفتوح      |
//+------------------------------------------------------------------+
#property copyright "zalfeni-sabri"
#property version   "1.00"
#property description "Drop it on a chart and the zones below are drawn on it"
#property description "اسحبه على الشارت وتتّرسم المناطق اللي تحت"
#property script_show_inputs

//--- one zone per line:  <bottom> <top> <buy|sell|fvg> <label>
input string InpZone1 = "4423 4431 sell Bear OB";
input string InpZone2 = "4395 4402 sell Bear OB";
input string InpZone3 = "4327 4345 fvg  Imbalance / FVG";
input string InpZone4 = "4298 4318 buy  Bull OB";
input string InpZone5 = "";
input string InpZone6 = "";
input string InpZone7 = "";
input string InpZone8 = "";

input group  "=== Look / الشكل ==="
input int    InpBarsBack   = 300;               // how far left the boxes start (بداية المربّع)
input int    InpExtendBars = 8;                 // extend to the right (مدّ لليمين)
input bool   InpFill       = true;              // filled zone (تعبئة)
input int    InpBorderWidth= 2;                 // border width (سماكة الإطار)
input int    InpFontSize   = 9;                 // label size
input color  InpBuyFill    = C'182,218,200';    // demand fill
input color  InpBuyBorder  = C'31,138,81';      // demand border
input color  InpSellFill   = C'242,194,194';    // supply fill
input color  InpSellBorder = C'178,58,58';      // supply border
input color  InpFvgColor   = C'122,140,190';    // FVG outline
input color  InpTextColor  = C'120,120,120';    // labels
input bool   InpClearOld   = true;              // delete zones drawn before (مسح القديم)

#define PFX "MANUAL_OB_"

//+------------------------------------------------------------------+
void DrawZone(const int idx,const double bottom,const double top,
              const string side,const string label,
              const datetime tLeft,const datetime tRight)
  {
   string name = PFX+(string)idx;
   bool   isFvg  = (side=="fvg");
   bool   isSell = (side=="sell");
   color  fill   = isSell ? InpSellFill   : InpBuyFill;
   color  border = isFvg  ? InpFvgColor   : (isSell ? InpSellBorder : InpBuyBorder);

//--- soft body behind the candles
   if(InpFill && !isFvg)
     {
      string bg = name+"_bg";
      ObjectCreate(0,bg,OBJ_RECTANGLE,0,tLeft,top,tRight,bottom);
      ObjectSetInteger(0,bg,OBJPROP_FILL,true);
      ObjectSetInteger(0,bg,OBJPROP_BACK,true);
      ObjectSetInteger(0,bg,OBJPROP_COLOR,fill);
      ObjectSetInteger(0,bg,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,bg,OBJPROP_HIDDEN,true);
     }

//--- crisp border in front
   ObjectCreate(0,name,OBJ_RECTANGLE,0,tLeft,top,tRight,bottom);
   ObjectSetInteger(0,name,OBJPROP_FILL,false);
   ObjectSetInteger(0,name,OBJPROP_BACK,false);
   ObjectSetInteger(0,name,OBJPROP_COLOR,border);
   ObjectSetInteger(0,name,OBJPROP_WIDTH,isFvg ? 1 : MathMax(1,InpBorderWidth));
   ObjectSetInteger(0,name,OBJPROP_STYLE,isFvg ? STYLE_DOT : STYLE_SOLID);
   ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
   ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);

//--- label on the right edge
   string tx = name+"_tx";
   ObjectCreate(0,tx,OBJ_TEXT,0,tRight,(top+bottom)/2.0);
   ObjectSetString(0,tx,OBJPROP_TEXT," "+label+"  "+
                   DoubleToString(bottom,_Digits)+" - "+DoubleToString(top,_Digits));
   ObjectSetString(0,tx,OBJPROP_FONT,"Arial");
   ObjectSetInteger(0,tx,OBJPROP_FONTSIZE,MathMax(6,InpFontSize));
   ObjectSetInteger(0,tx,OBJPROP_COLOR,isFvg ? InpFvgColor : border);
   ObjectSetInteger(0,tx,OBJPROP_ANCHOR,ANCHOR_LEFT);
   ObjectSetInteger(0,tx,OBJPROP_SELECTABLE,false);
   ObjectSetInteger(0,tx,OBJPROP_HIDDEN,true);
  }

//+------------------------------------------------------------------+
bool ParseZone(const string raw,double &bottom,double &top,string &side,string &label)
  {
   string s = raw;
   StringTrimLeft(s);
   StringTrimRight(s);
   if(StringLen(s)==0)
      return(false);

   string part[];
   int n = StringSplit(s,' ',part);
   if(n<3)
     {
      Print("سطر غير صالح: ",raw);
      return(false);
     }

   int    got = 0;
   double v[2] = {0.0,0.0};
   int    i    = 0;
   for(; i<n && got<2; i++)
     {
      if(StringLen(part[i])==0)
         continue;
      v[got++] = StringToDouble(part[i]);
     }
   if(got<2 || v[0]<=0.0 || v[1]<=0.0)
     {
      Print("ما لقيتش السعرين في: ",raw);
      return(false);
     }

   side = "";
   for(; i<n; i++)
     {
      if(StringLen(part[i])==0)
         continue;
      side = part[i];
      StringToLower(side);
      i++;
      break;
     }
   if(side!="buy" && side!="sell" && side!="fvg")
     {
      Print("النوع لازم buy أو sell أو fvg، لقيت: ",side);
      return(false);
     }

   label = "";
   for(; i<n; i++)
      if(StringLen(part[i])>0)
         label += (StringLen(label)>0 ? " " : "")+part[i];
   if(StringLen(label)==0)
      label = (side=="fvg" ? "FVG" : (side=="sell" ? "Bear OB" : "Bull OB"));

   bottom = MathMin(v[0],v[1]);
   top    = MathMax(v[0],v[1]);
   return(true);
  }

//+------------------------------------------------------------------+
void OnStart()
  {
   if(InpClearOld)
      ObjectsDeleteAll(0,PFX);

   int bars = Bars(_Symbol,_Period);
   if(bars<10)
     {
      Print("ما فماش شموع كافية على الشارت");
      return;
     }

   datetime tLeft  = iTime(_Symbol,_Period,MathMin(MathMax(10,InpBarsBack),bars-1));
   datetime tRight = (datetime)(iTime(_Symbol,_Period,0)+
                                PeriodSeconds()*MathMax(1,InpExtendBars));

   string raws[8];
   raws[0]=InpZone1; raws[1]=InpZone2; raws[2]=InpZone3; raws[3]=InpZone4;
   raws[4]=InpZone5; raws[5]=InpZone6; raws[6]=InpZone7; raws[7]=InpZone8;

   int drawn = 0;
   for(int i=0; i<8; i++)
     {
      double bottom=0.0, top=0.0;
      string side="", label="";
      if(!ParseZone(raws[i],bottom,top,side,label))
         continue;
      DrawZone(i,bottom,top,side,label,tLeft,tRight);
      drawn++;
     }

   ChartRedraw();
   Print("تمّ رسم ",drawn," منطقة على ",_Symbol," ",EnumToString((ENUM_TIMEFRAMES)Period()));
  }
//+------------------------------------------------------------------+

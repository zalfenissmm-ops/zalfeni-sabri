#include "mql5_mock.h"
//--- context the mock needs
int    _Digits = 2;
double _Point  = 0.01;
std::string _Symbol = "XAUUSD";
ENUM_TIMEFRAMES _Period = PERIOD_H1;
int    g_periodSeconds = 3600;
color  g_chartBackground = MQLCOLOR(255,255,255);   // white chart, like the user's
std::map<std::string,MockObject> g_objects;
std::vector<std::string> g_objectOrder;

#include "indicator.inc"     // <-- the real indicator source, unmodified logic
#include <random>

static MqlArray<datetime> T; static MqlArray<double> O,H,L,C;
static MqlArray<long> TV,V; static MqlArray<int> SP;

static void push(double o,double h,double l,double c){
  int n=ArraySize(O);
  ArrayResize(T,n+1); ArrayResize(O,n+1); ArrayResize(H,n+1); ArrayResize(L,n+1); ArrayResize(C,n+1);
  ArrayResize(TV,n+1); ArrayResize(V,n+1); ArrayResize(SP,n+1);
  T[n]=1757000000LL+(datetime)n*3600; O[n]=o; H[n]=h; L[n]=l; C[n]=c; TV[n]=100; V[n]=100; SP[n]=2;
}
static void reset(){ ArrayResize(T,0);ArrayResize(O,0);ArrayResize(H,0);ArrayResize(L,0);
                     ArrayResize(C,0);ArrayResize(TV,0);ArrayResize(V,0);ArrayResize(SP,0);
                     g_objects.clear(); g_objectOrder.clear(); }

static void run(){ OnInit(); OnCalculate(ArraySize(O),0,T,O,H,L,C,TV,V,SP); }

static void printSetups(const char* title){
  printf("\n=== %s: %d setup(s) ===\n", title, ArraySize(g_setups));
  int wins=0,losses=0,open_=0; double sumR=0;
  for(int i=0;i<ArraySize(g_setups);i++){
    TradeSetup s=g_setups[i];
    double risk = s.bullish? s.entry-s.sl : s.sl-s.entry;
    double rew  = s.bullish? s.tp-s.entry : s.entry-s.tp;
    if(s.result==RES_TARGET){wins++; sumR+=rew/risk;}
    else if(s.result==RES_STOP){losses++; sumR-=1.0;}
    else open_++;
    if(ArraySize(g_setups)<=8)
      printf("  %-4s sweep@%4d entry@%4d | LQ %8.2f BOS %8.2f | entry %8.2f SL %8.2f TP1 %8.2f TP %8.2f | %.2fR | %s\n",
             s.bullish?"BUY":"SELL", s.sweepIdx, s.entryIdx, s.lq, s.bos, s.entry, s.sl, s.tp1, s.tp,
             rew/risk, s.result==RES_TARGET?"TARGET":(s.result==RES_STOP?"STOP":"open"));
  }
  if(ArraySize(g_setups)>8)
    printf("  filled %d (wins %d / losses %d) | open %d | win rate %.1f%% | total %+.1fR | expectancy %+.2fR/trade\n",
           wins+losses,wins,losses,open_, wins+losses? 100.0*wins/(wins+losses):0.0, sumR, (wins+losses)? sumR/(wins+losses):0.0);
  else
    printf("  wins %d | losses %d | open %d | total %+.1fR\n",wins,losses,open_,sumR);
}

static void printObjects(){
  printf("  drawn objects (%d):\n",(int)g_objects.size());
  for(auto&kv:g_objects){
    const MockObject&o=kv.second;
    const char* t = o.type==OBJ_RECTANGLE?"BOX ":o.type==OBJ_TREND?"LINE":o.type==OBJ_TEXT?"TEXT":"LBL ";
    if(o.type==OBJ_TEXT||o.type==OBJ_LABEL) printf("    %-4s %-22s \"%s\"\n",t,kv.first.c_str(),o.text.c_str());
    else printf("    %-4s %-22s %.2f .. %.2f\n",t,kv.first.c_str(),o.p1,o.p2);
  }
}

//--------------------------------------------------------------------------
// 1) the video's setup, candle by candle
//--------------------------------------------------------------------------
static void scenarioVideo(){
  reset();
  for(int i=0;i<6;i++) push(4310+i,4316+i,4306+i,4314+i);
  push(4320,4345,4318,4344);                 // candle 1 of the gap (high 4345)
  push(4344,4398,4343,4390);                 // impulse
  push(4390,4400,4362,4396);                 // candle 3 (low 4362) -> FVG 4345/4362
  for(int i=0;i<8;i++) push(4396+i*8,4404+i*8,4392+i*8,4402+i*8);
  push(4466,4500,4462,4480);
  for(int i=0;i<10;i++) push(4480-i*8,4486-i*8,4472-i*8,4474-i*8);
  push(4404,4406,4396,4399);
  push(4396,4397.83,4390,4392);               // swing high 4397.83  -> BOS
  push(4392,4394,4386,4388);
  push(4388,4390,4378,4380);
  push(4380,4382,4370,4372);
  push(4372,4374,4364.16,4368);               // swing low 4364.16   -> LQ
  push(4368,4380,4366,4378);
  push(4378,4392,4376,4390);
  push(4390,4397.50,4386,4388);
  push(4388,4390,4380,4382);
  push(4382,4384,4372,4374);
  push(4374,4376,4366,4368);
  push(4368,4370,4347.00,4357.82);            // sweep into the FVG
  push(4357.82,4375,4356,4374.42);            // reclaim -> entry
  push(4374,4380,4370,4379.36);
  push(4379,4388,4377,4386.56);
  push(4386,4396,4384,4394);
  push(4394,4402.89,4392,4402.36);            // target
  for(int i=0;i<5;i++) push(4402,4408,4398,4404);
  run();
  printSetups("video scenario");
  printObjects();
}

//--------------------------------------------------------------------------
// 2) 6000 synthetic gold-like H1 bars (random walk + volatility clustering)
//--------------------------------------------------------------------------
static void scenarioRandom(int bars,unsigned seed){
  reset();
  std::mt19937 rng(seed);
  std::normal_distribution<double> z(0.0,1.0);
  double price=4000.0, vol=3.0;
  for(int i=0;i<bars;i++){
    vol = 0.94*vol + 0.06*(2.0+4.0*std::fabs(z(rng)));       // clustered volatility
    double o=price;
    double c=o+z(rng)*vol;
    double h=std::max(o,c)+std::fabs(z(rng))*vol*0.7;
    double l=std::min(o,c)-std::fabs(z(rng))*vol*0.7;
    push(o,h,l,c);
    price=c;
    if(price<500) price=500;
  }
  run();
  char title[64]; snprintf(title,64,"random walk, %d bars, seed %u",bars,seed);
  printSetups(title);
  printf("  objects on chart: %d\n",(int)g_objects.size());
}

static double g_sumR=0; static int g_trades=0, g_wins=0;
int main(){
  InpLookbackBars = 100000;      // analyse the whole test series
  scenarioVideo();
  for(unsigned s=1;s<=12;s++){
    scenarioRandom(6000,s);
    for(int i=0;i<ArraySize(g_setups);i++){
      TradeSetup t=g_setups[i];
      double risk=t.bullish?t.entry-t.sl:t.sl-t.entry, rew=t.bullish?t.tp-t.entry:t.entry-t.tp;
      if(t.result==RES_TARGET){ g_sumR+=rew/risk; g_trades++; g_wins++; }
      else if(t.result==RES_STOP){ g_sumR-=1.0; g_trades++; }
    }
  }
  printf("\n===== 12 random-walk runs, 72000 bars =====\n");
  printf("trades %d | win rate %.1f%% | total %+.1fR | expectancy %+.3fR per trade\n",
         g_trades,100.0*g_wins/g_trades,g_sumR,g_sumR/g_trades);
  printf("(a driftless random walk has no edge: an honest evaluation must land near 0.000R)\n");
  return 0;
}

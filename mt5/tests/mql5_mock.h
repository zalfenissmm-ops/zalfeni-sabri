// Minimal MQL5 runtime mock so the real .mq5 source can be compiled and run
// with g++. Records every drawing call so a harness can inspect the output.
#pragma once
#include <string>
#include <vector>
#include <map>
#include <cmath>
#include <cstdio>
#include <cstdarg>
#include <algorithm>

typedef long long datetime;
typedef unsigned int color;
typedef long long_t;

#define MQLCOLOR(r,g,b) ((color)((r) | ((g)<<8) | ((b)<<16)))
static const color clrCrimson=MQLCOLOR(220,20,60), clrKhaki=MQLCOLOR(240,230,140),
  clrLime=MQLCOLOR(0,255,0), clrLimeGreen=MQLCOLOR(50,205,50), clrRoyalBlue=MQLCOLOR(65,105,225),
  clrSilver=MQLCOLOR(192,192,192), clrTomato=MQLCOLOR(255,99,71), clrWhite=MQLCOLOR(255,255,255),
  clrBlack=MQLCOLOR(0,0,0);

enum ENUM_OBJECT { OBJ_RECTANGLE, OBJ_TREND, OBJ_TEXT, OBJ_LABEL };
enum ENUM_LINE_STYLE { STYLE_SOLID, STYLE_DASH, STYLE_DOT };
enum ENUM_ANCHOR_POINT { ANCHOR_LEFT, ANCHOR_RIGHT_UPPER };
enum { CORNER_LEFT_UPPER=0 };
enum { OBJPROP_COLOR, OBJPROP_STYLE, OBJPROP_WIDTH, OBJPROP_BACK, OBJPROP_FILL,
       OBJPROP_SELECTABLE, OBJPROP_SELECTED, OBJPROP_HIDDEN, OBJPROP_ZORDER,
       OBJPROP_RAY_RIGHT, OBJPROP_TEXT, OBJPROP_FONT, OBJPROP_FONTSIZE,
       OBJPROP_ANCHOR, OBJPROP_CORNER, OBJPROP_XDISTANCE, OBJPROP_YDISTANCE };
enum { CHART_COLOR_BACKGROUND };
enum { INIT_SUCCEEDED=0 };
enum ENUM_TIMEFRAMES { PERIOD_H1=16385 };
enum { INDICATOR_SHORTNAME=0 };

//--- dynamic arrays -------------------------------------------------------
template<class T> struct MqlArray {
  std::vector<T> v;
  T&       operator[](int i)       { return v[(size_t)i]; }
  const T& operator[](int i) const { return v[(size_t)i]; }
};
template<class T> int  ArraySize(const MqlArray<T>&a){ return (int)a.v.size(); }
template<class T> int  ArrayResize(MqlArray<T>&a,int n){ a.v.resize((size_t)n); return n; }
template<class T> bool ArraySetAsSeries(const MqlArray<T>&,bool){ return true; }

//--- math / strings -------------------------------------------------------
inline double MathAbs(double x){ return std::fabs(x); }
inline double MathMin(double a,double b){ return a<b?a:b; }
inline double MathMax(double a,double b){ return a>b?a:b; }
inline std::string IntegerToString(long long v){ char b[64]; snprintf(b,64,"%lld",v); return b; }
inline std::string DoubleToString(double v,int d){ char b[64]; snprintf(b,64,"%.*f",d,v); return b; }
inline std::string EnumToString(ENUM_TIMEFRAMES){ return "PERIOD_H1"; }
inline std::string StringFormat(const char*f,...){
  char b[1024]; va_list ap; va_start(ap,f); vsnprintf(b,sizeof(b),f,ap); va_end(ap); return b; }
inline std::string StringFormat(const char*f,std::string a,std::string b,std::string c,
                                std::string d,std::string e,std::string g){
  char o[1024]; snprintf(o,sizeof(o),f,a.c_str(),b.c_str(),c.c_str(),d.c_str(),e.c_str(),g.c_str());
  return o; }

//--- symbol / chart context ----------------------------------------------
extern int    _Digits;
extern double _Point;
extern std::string _Symbol;
extern ENUM_TIMEFRAMES _Period;
extern int    g_periodSeconds;
extern color  g_chartBackground;
inline int  PeriodSeconds(){ return g_periodSeconds; }
inline long long ChartGetInteger(long,int prop,int=0){ return prop==CHART_COLOR_BACKGROUND?(long long)g_chartBackground:0; }
inline void ChartRedraw(long=0){}
inline bool IndicatorSetString(int,std::string){ return true; }
inline void Alert(std::string s){ printf("ALERT: %s\n",s.c_str()); }
inline bool SendNotification(std::string){ return true; }

//--- recorded chart objects ----------------------------------------------
struct MockObject {
  std::string name; ENUM_OBJECT type;
  datetime t1=0,t2=0; double p1=0,p2=0;
  std::string text; long long clr=0; int style=0;
};
extern std::map<std::string,MockObject> g_objects;
extern std::vector<std::string> g_objectOrder;

inline bool ObjectCreate(long,std::string name,ENUM_OBJECT type,int,
                         datetime t1=0,double p1=0,datetime t2=0,double p2=0){
  MockObject o; o.name=name; o.type=type; o.t1=t1; o.p1=p1; o.t2=t2; o.p2=p2;
  if(!g_objects.count(name)) g_objectOrder.push_back(name);
  g_objects[name]=o; return true; }
inline int  ObjectFind(long,std::string name){ return g_objects.count(name)?0:-1; }
inline bool ObjectMove(long,std::string name,int pt,datetime t,double p){
  if(!g_objects.count(name)) return false;
  if(pt==0){ g_objects[name].t1=t; g_objects[name].p1=p; }
  else     { g_objects[name].t2=t; g_objects[name].p2=p; }
  return true; }
inline bool ObjectSetInteger(long,std::string name,int prop,long long val){
  if(!g_objects.count(name)) return false;
  if(prop==OBJPROP_COLOR) g_objects[name].clr=val;
  if(prop==OBJPROP_STYLE) g_objects[name].style=(int)val;
  return true; }
inline bool ObjectSetString(long,std::string name,int prop,std::string val){
  if(!g_objects.count(name)) return false;
  if(prop==OBJPROP_TEXT) g_objects[name].text=val;
  return true; }
inline int ObjectsDeleteAll(long,std::string prefix,int=-1,int=-1){
  int n=0;
  for(auto it=g_objects.begin(); it!=g_objects.end(); ){
    if(it->first.rfind(prefix,0)==0){ it=g_objects.erase(it); n++; } else ++it; }
  g_objectOrder.clear();
  return n; }

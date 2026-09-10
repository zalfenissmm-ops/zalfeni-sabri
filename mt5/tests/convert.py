"""Translate the .mq5 source into compilable C++ (mechanical rewrites only)."""
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
out = []
for line in src.split("\n"):
    if line.strip().startswith("#property"):
        continue
    out.append(line)
s = "\n".join(out)
s = re.sub(r"\bC'(\d+),(\d+),(\d+)'", r"MQLCOLOR(\1,\2,\3)", s)       # colour literals
s = re.sub(r"^(\s*)input\s+", r"\1", s, flags=re.M)                   # inputs -> globals
s = re.sub(r"\bstring\b", "std::string", s)
s = re.sub(r"(const\s+)?(\w+)\s*&\s*(\w+)\s*\[\s*\]", r"\1MqlArray<\2>& \3", s)  # array params
s = re.sub(r"^(\s*)(\w+)\s+(g_\w+)\s*\[\s*\]\s*;", r"\1MqlArray<\2> \3;", s, flags=re.M)  # array globals
s = s.replace("#define PREFIX  \"SMCV_\"", "#define PREFIX  std::string(\"SMCV_\")")
open(sys.argv[2], "w", encoding="utf-8").write(s)
print("converted ->", sys.argv[2])

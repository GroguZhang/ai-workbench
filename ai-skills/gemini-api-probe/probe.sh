#!/usr/bin/env bash
# Gemini API 探针：验活 Key、拉可用模型、真实生成探针、SSE 流式连通性
# 用法: bash probe.sh "AQ.xxxx / AIza..." [模型名]
# 不改动任何项目文件，全部探测都走只读接口 + 一次极短生成。

set -u
KEY="${1:-}"
MODEL="${2:-gemini-3.8-flash}"
API="https://generativelanguage.googleapis.com/v1beta/models"
TMP="_gemini_probe"
mkdir -p "$TMP"

if [ -z "$KEY" ]; then
  echo "用法: bash probe.sh \"你的KEY\" [模型名]"
  exit 1
fi

echo "=============================================="
echo " Gemini API 探针  |  模型: $MODEL"
echo "=============================================="

# ---------- 1. 列模型：确认 Key 是否被服务端接受 ----------
echo
echo "[1/4] 列模型（验证 Key 是否被接受）"
CODE=$(curl -s --ssl-no-revoke -m 30 -o "$TMP/models.json" -w "%{http_code}" \
  -H "x-goog-api-key: $KEY" "$API?pageSize=200")
echo "  HTTP $CODE"
if [ "$CODE" != "200" ]; then
  echo "  → Key 未被接受，原始响应："
  head -c 400 "$TMP/models.json"; echo
  if grep -q "User location is not supported" "$TMP/models.json"; then
    echo "  → 地域拦截：连列模型接口都会被拦，与 Key 无关。"
    echo "    需让 generativelanguage.googleapis.com 走受支持地区节点（日本/新加坡/美国）。"
  elif grep -q "API_KEY_INVALID\|API key not valid" "$TMP/models.json"; then
    echo "  → 密钥无效或已吊销，换 Key。"
  elif grep -q "PERMISSION_DENIED\|has been used\|not enabled" "$TMP/models.json"; then
    echo "  → 项目未启用 Generative Language API，或 Key 被来源限制。"
  fi
  exit 1
fi
echo "  ✅ Key 可用"

# ---------- 2. 可用模型清单 ----------
echo
echo "[2/4] 该 Key 可调用的 Gemini 模型"
node -e '
const fs=require("fs");
const j=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
const g=j.models.filter(m=>(m.supportedGenerationMethods||[]).includes("generateContent")&&/gemini/.test(m.name));
console.log("  共 "+g.length+" 个");
g.map(m=>m.name.replace("models/","")).forEach(n=>console.log("   · "+n));
const want=["gemini-3.8-flash","gemini-flash-latest","gemini-pro-latest","gemini-3.7-flash","gemini-3.5-flash","gemini-3.1-flash-lite"];
const miss=want.filter(n=>!g.some(m=>m.name==="models/"+n));
if(miss.length) console.log("\n  ⚠️ 常用模型不可用: "+miss.join(", "));
' "$TMP/models.json" 2>/dev/null || echo "  (需 node 解析，已跳过)"

# ---------- 3. 真实生成探针（关键：列模型能过不代表能生成）----------
echo
echo "[3/4] 真实生成探针（能列模型 ≠ 能生成，地域拦截常在此暴露）"
printf '%s' '{"contents":[{"role":"user","parts":[{"text":"只回复两个字：收到"}]}],"generationConfig":{"temperature":0,"maxOutputTokens":64}}' > "$TMP/body.json"
CODE=$(curl -s --ssl-no-revoke -m 45 -o "$TMP/gen.json" -w "%{http_code}" \
  -H "x-goog-api-key: $KEY" -H "Content-Type: application/json" \
  -X POST "$API/$MODEL:generateContent" -d @"$TMP/body.json")
echo "  HTTP $CODE"
case "$CODE" in
  200)
    node -e '
const fs=require("fs");const j=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
const c=(j.candidates||[])[0]||{};let t="";
((c.content&&c.content.parts)||[]).forEach(p=>{if(typeof p.text==="string"&&!p.thought)t+=p.text});
console.log("  ✅ 生成成功 | 回复: "+JSON.stringify(t.slice(0,20))+" | finish: "+(c.finishReason||"?"));
console.log("  usage: "+JSON.stringify(j.usageMetadata||{}));
' "$TMP/gen.json" 2>/dev/null || echo "  ✅ 200（解析跳过）"
    ;;
  400)
    MSG=$(node -e 'try{console.log(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).error.message)}catch(e){console.log("")}' "$TMP/gen.json" 2>/dev/null)
    echo "  ❌ $MSG"
    case "$MSG" in
      *"User location is not supported"*)
        echo "  → 地域拦截：与 Key 无关。需让 generativelanguage.googleapis.com 走受支持地区节点（日本/新加坡/美国）。"
        echo "    国内直连、香港节点通常被拒。走 OpenClash/Clash 请确认该域名命中受支持地区策略组而非 DIRECT。" ;;
      *"API key"*|*"API_KEY"*)
        echo "  → 密钥问题：换 Key 或检查项目权限。" ;;
      *"thinking"*|*"generationConfig"*)
        echo "  → 参数不兼容：试剥离 thinkingConfig 后重发。" ;;
    esac
    ;;
  404)
    MSG=$(node -e 'try{console.log(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).error.message)}catch(e){console.log("")}' "$TMP/gen.json" 2>/dev/null)
    echo "  ❌ $MSG"
    echo "  → 模型已下线，换新模型（多数情况下按提示切换到 gemini-3.8-flash 即可）。"
    ;;
  503) echo "  ❌ 服务端过载（high demand），稍后重试或换模型。" ;;
  429) echo "  ❌ 限流/配额，稍等 1 分钟。" ;;
  *)   head -c 300 "$TMP/gen.json"; echo ;;
esac

# ---------- 4. SSE 流式连通性 ----------
echo
echo "[4/4] SSE 流式连通性（streamGenerateContent）"
printf '%s' '{"contents":[{"role":"user","parts":[{"text":"只回复两个字：收到"}]}],"generationConfig":{"temperature":0,"maxOutputTokens":64}}' > "$TMP/body2.json"
CODE=$(curl -s --ssl-no-revoke -m 45 -N -o "$TMP/sse.txt" -w "%{http_code}" \
  -H "x-goog-api-key: $KEY" -H "Content-Type: application/json" \
  -X POST "$API/$MODEL:streamGenerateContent?alt=sse" -d @"$TMP/body2.json")
echo "  HTTP $CODE"
if [ "$CODE" = "200" ]; then
  node -e '
const fs=require("fs");const t=fs.readFileSync(process.argv[1],"utf8");
const chunks=t.split("\n").filter(l=>l.trim().startsWith("data:")).map(l=>{try{return JSON.parse(l.trim().slice(5))}catch(e){return null}}).filter(Boolean);
let full="",th=0;
chunks.forEach(j=>{const c=(j.candidates||[])[0];if(!c)return;((c.content&&c.content.parts)||[]).forEach(p=>{if(typeof p.text!=="string")return;if(p.thought)th+=p.text.length;else full+=p.text})});
console.log("  ✅ SSE 正常 | 事件块 "+chunks.length+" | 可见文本 "+JSON.stringify(full.slice(0,20))+" | thought 字符 "+th);
' "$TMP/sse.txt" 2>/dev/null || echo "  ✅ 200"
else
  head -c 200 "$TMP/sse.txt"; echo
fi

rm -rf "$TMP"
echo
echo "探针结束。"

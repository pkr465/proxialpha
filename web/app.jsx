import { useState, useEffect, useCallback, useRef, useMemo, memo } from "react";
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, PieChart, Pie, Cell, RadarChart,
  Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  ComposedChart, Scatter
} from "recharts";

// ============================================================
// PROXIALPHA — AI TRADING TERMINAL (Module Version)
// ProxiAlpha professional trading dashboard
// Connects to FastAPI backend via REST + WebSocket
// ============================================================

// ---- DESIGN TOKENS ----
const C = {
  bg:          "#0a0d14",
  bgSecondary: "#0e1219",
  surface:     "#111620",
  surfaceAlt:  "#151a26",
  card:        "#161c2a",
  cardHover:   "#1a2133",
  border:      "#1e2536",
  borderLight: "#2a3145",
  borderFocus: "#3d4a66",
  text:        "#e8eaed",
  textSoft:    "#b0b5c3",
  textMuted:   "#6b7280",
  textDim:     "#454d5e",
  bullish:     "#26a69a",
  bullishDim:  "rgba(38,166,154,0.12)",
  bullishMid:  "rgba(38,166,154,0.25)",
  bearish:     "#ef5350",
  bearishDim:  "rgba(239,83,80,0.12)",
  bearishMid:  "rgba(239,83,80,0.25)",
  accent:      "#2962ff",
  accentDim:   "rgba(41,98,255,0.12)",
  accentMid:   "rgba(41,98,255,0.25)",
  purple:      "#7c3aed",
  purpleDim:   "rgba(124,58,237,0.12)",
  yellow:      "#f59e0b",
  yellowDim:   "rgba(245,158,11,0.12)",
  cyan:        "#06b6d4",
  orange:      "#f97316",
  pink:        "#ec4899",
};

const STRAT_COLORS = {
  DipBuyer: C.accent, Technical: C.purple, Momentum: C.bullish,
  MeanReversion: C.cyan, Breakout: C.orange, TrendFollowing: C.yellow,
  DCA: "#84CC16", PairsTrading: C.pink, EarningsPlay: "#F43F5E",
  SectorRotation: "#14B8A6", Scalping: "#A855F7", SwingTrading: "#6366F1",
  OptionsFlow: "#E11D48", CustomRules: C.textMuted, AI_Claude: "#FF6B35",
};

// ---- SVG ICONS (Lucide-style) ----
const Icons = {
  chart: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>,
  zap: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>,
  activity: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
  brain: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>,
  target: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>,
  shield: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
  settings: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>,
  panelLeft: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/></svg>,
  wallet: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/></svg>,
  candlestick: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M9 16v6"/><path d="M15 16v6"/><rect x="7" y="8" width="4" height="8" rx="1"/><rect x="13" y="8" width="4" height="8" rx="1"/></svg>,
  refresh: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>,
  play: (s=16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>,
  trendUp: (s=12) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>,
  trendDown: (s=12) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 17 13.5 8.5 8.5 13.5 2 7"/><polyline points="16 17 22 17 22 11"/></svg>,
};

// ---- SAMPLE DATA ----
const STRATEGIES_DATA = [
  { name: "DipBuyer", active: true, weight: 1.2, category: "Value", description: "Buy pullbacks from ATH" },
  { name: "Technical", active: true, weight: 1.0, category: "Technical", description: "RSI / MACD / MA scoring" },
  { name: "Momentum", active: true, weight: 1.0, category: "Trend", description: "ROC + relative strength" },
  { name: "MeanReversion", active: true, weight: 0.9, category: "Value", description: "Z-score + Bollinger" },
  { name: "Breakout", active: false, weight: 1.0, category: "Trend", description: "Volume-confirmed breakouts" },
  { name: "TrendFollowing", active: false, weight: 1.1, category: "Trend", description: "Multi-TF MA + ADX" },
  { name: "DCA", active: false, weight: 0.8, category: "Accumulation", description: "Dollar-cost averaging" },
  { name: "PairsTrading", active: false, weight: 0.7, category: "Arbitrage", description: "Statistical arb" },
  { name: "EarningsPlay", active: false, weight: 0.8, category: "Event", description: "Earnings drift" },
  { name: "SectorRotation", active: false, weight: 0.9, category: "Macro", description: "Sector momentum" },
  { name: "Scalping", active: false, weight: 0.6, category: "Short-Term", description: "Micro reversals" },
  { name: "SwingTrading", active: false, weight: 1.0, category: "Short-Term", description: "S/R + Fibonacci" },
  { name: "OptionsFlow", active: false, weight: 0.8, category: "Flow", description: "Unusual volume" },
  { name: "AI_Claude", active: false, weight: 1.5, category: "AI", description: "LLM signal gen" },
];

const SIGNALS_DATA = [
  { Ticker: "EL", Signal: "STRONG_BUY", Score: 1.45, Confidence: 0.88, Target: 279.75, Stop: 102.31, Price: 156.20, Change: -2.8 },
  { Ticker: "NKE", Signal: "STRONG_BUY", Score: 1.32, Confidence: 0.85, Target: 134.25, Stop: 58.13, Price: 71.40, Change: 1.2 },
  { Ticker: "HIMS", Signal: "BUY", Score: 0.98, Confidence: 0.76, Target: 54.00, Stop: 18.50, Price: 25.80, Change: 4.5 },
  { Ticker: "COIN", Signal: "BUY", Score: 0.95, Confidence: 0.74, Target: 332.25, Stop: 147.76, Price: 215.30, Change: -1.1 },
  { Ticker: "TGT", Signal: "BUY", Score: 0.82, Confidence: 0.72, Target: 201.75, Stop: 106.65, Price: 132.50, Change: 0.8 },
  { Ticker: "NVO", Signal: "BUY", Score: 0.78, Confidence: 0.71, Target: 111.00, Stop: 42.00, Price: 68.20, Change: -0.4 },
  { Ticker: "ETH", Signal: "BUY", Score: 0.72, Confidence: 0.68, Target: 3717.00, Stop: 2246.59, Price: 2580.00, Change: 3.2 },
  { Ticker: "SOFI", Signal: "HOLD", Score: 0.42, Confidence: 0.58, Target: null, Stop: null, Price: 14.20, Change: 0.3 },
  { Ticker: "ORCL", Signal: "HOLD", Score: 0.35, Confidence: 0.55, Target: null, Stop: null, Price: 178.50, Change: -0.7 },
  { Ticker: "HOOD", Signal: "HOLD", Score: 0.22, Confidence: 0.52, Target: null, Stop: null, Price: 24.30, Change: 2.1 },
  { Ticker: "MSTR", Signal: "HOLD", Score: 0.15, Confidence: 0.50, Target: null, Stop: null, Price: 1685.00, Change: -3.4 },
  { Ticker: "LULU", Signal: "HOLD", Score: 0.10, Confidence: 0.48, Target: null, Stop: null, Price: 402.30, Change: 0.1 },
  { Ticker: "UNH", Signal: "HOLD", Score: 0.05, Confidence: 0.45, Target: null, Stop: null, Price: 524.80, Change: -0.9 },
];

const EQUITY = Array.from({ length: 180 }, (_, i) => ({
  day: i + 1,
  portfolio: Math.round(100000 + Math.sin(i / 15) * 4000 + i * 95 + (Math.random() - 0.3) * 1500),
  benchmark: Math.round(100000 + i * 45 + (Math.random() - 0.5) * 800),
}));

// ---- API CLIENT ----
const API_BASE = typeof window !== "undefined" ? window.location.origin : "";
async function apiGet(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`);
    if (!res.ok) throw new Error(`${res.status}`);
    return await res.json();
  } catch (e) { console.warn(`API GET ${path}:`, e); return null; }
}
async function apiPost(path, body) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status}`);
    return await res.json();
  } catch (e) { console.warn(`API POST ${path}:`, e); return null; }
}

// ---- UTILITY ----
const fmt = (n, d=2) => n != null ? Number(n).toFixed(d) : "\u2014";
const fmtK = (n) => n >= 1e6 ? `$${(n/1e6).toFixed(1)}M` : n >= 1e3 ? `$${(n/1e3).toFixed(1)}K` : `$${n}`;
const fmtPct = (n) => n != null ? `${n > 0 ? "+" : ""}${Number(n).toFixed(2)}%` : "\u2014";

// ---- COMPONENTS ----
const Spinner = ({size=16}) => (
  <div style={{ width: size, height: size, border: `2px solid ${C.border}`, borderTopColor: C.accent, borderRadius: "50%", animation: "spin 0.6s linear infinite" }} />
);

const MetricCard = ({ label, value, sub, color, trend, icon }) => (
  <div style={{
    background: C.card, borderRadius: 8, padding: "14px 16px", flex: 1, minWidth: 150,
    border: `1px solid ${C.border}`, position: "relative", overflow: "hidden",
  }}>
    <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 2, background: color || C.accent, opacity: 0.5 }} />
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
      <span style={{ fontSize: 11, fontWeight: 500, color: C.textMuted, letterSpacing: 0.6, textTransform: "uppercase" }}>{label}</span>
      {icon && <span style={{ color: C.textDim }}>{icon}</span>}
    </div>
    <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
      <span style={{ fontSize: 22, fontWeight: 700, color: color || C.text, lineHeight: 1, fontFamily: "'JetBrains Mono', monospace", fontVariantNumeric: "tabular-nums" }}>{value}</span>
      {trend != null && (
        <span style={{
          display: "flex", alignItems: "center", gap: 2, fontSize: 11, fontWeight: 600,
          color: trend > 0 ? C.bullish : C.bearish,
          background: trend > 0 ? C.bullishDim : C.bearishDim,
          padding: "2px 6px", borderRadius: 4,
        }}>
          {trend > 0 ? Icons.trendUp(10) : Icons.trendDown(10)}
          {Math.abs(trend)}%
        </span>
      )}
    </div>
    {sub && <div style={{ fontSize: 11, color: C.textDim, marginTop: 4 }}>{sub}</div>}
  </div>
);

const SignalBadge = ({ signal }) => {
  const config = {
    STRONG_BUY: { bg: C.bullishDim, color: C.bullish, border: C.bullishMid },
    BUY: { bg: "rgba(38,166,154,0.08)", color: "#5ec4b6", border: "rgba(38,166,154,0.15)" },
    HOLD: { bg: "rgba(107,114,128,0.08)", color: C.textMuted, border: "rgba(107,114,128,0.15)" },
    SELL: { bg: "rgba(239,83,80,0.08)", color: "#f09090", border: "rgba(239,83,80,0.15)" },
    STRONG_SELL: { bg: C.bearishDim, color: C.bearish, border: C.bearishMid },
  };
  const c = config[signal] || config.HOLD;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "3px 10px", borderRadius: 4, fontSize: 10, fontWeight: 700,
      background: c.bg, color: c.color, border: `1px solid ${c.border}`,
      letterSpacing: 0.8, textTransform: "uppercase",
      fontFamily: "'JetBrains Mono', monospace",
    }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: c.color, opacity: 0.8 }} />
      {(signal || "").replace("_", " ")}
    </span>
  );
};

const ConfidenceBar = ({ value }) => {
  const pct = (value * 100);
  const color = pct > 75 ? C.bullish : pct > 55 ? C.yellow : C.bearish;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 100 }}>
      <div style={{ flex: 1, height: 4, borderRadius: 2, background: C.border, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", borderRadius: 2, background: color, transition: "width 0.6s ease" }} />
      </div>
      <span style={{ fontSize: 11, color: C.textSoft, minWidth: 32, fontFamily: "'JetBrains Mono', monospace" }}>{pct.toFixed(0)}%</span>
    </div>
  );
};

const Toggle = ({ on, onChange }) => (
  <div onClick={onChange} style={{
    width: 36, height: 20, borderRadius: 10, cursor: "pointer",
    background: on ? C.bullish : C.borderLight, transition: "background 0.2s",
    position: "relative", flexShrink: 0,
  }}>
    <div style={{
      width: 16, height: 16, borderRadius: 8, background: "#fff",
      position: "absolute", top: 2, left: on ? 18 : 2,
      transition: "left 0.2s cubic-bezier(0.4, 0, 0.2, 1)",
      boxShadow: "0 1px 3px rgba(0,0,0,0.3)",
    }} />
  </div>
);

const SectionHeader = ({ title, subtitle, action, icon }) => (
  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      {icon && <span style={{ color: C.textDim }}>{icon}</span>}
      <div>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: C.text, letterSpacing: -0.2 }}>{title}</h2>
        {subtitle && <p style={{ margin: "2px 0 0", fontSize: 11, color: C.textDim }}>{subtitle}</p>}
      </div>
    </div>
    {action}
  </div>
);

// ---- STRATEGY PANEL ----
const StrategyPanel = ({ strategies, onToggle }) => {
  const categories = {};
  strategies.forEach(s => {
    const cat = s.category || "Other";
    categories[cat] = categories[cat] || [];
    categories[cat].push(s);
  });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {Object.entries(categories).map(([cat, strats]) => (
        <div key={cat}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textDim, textTransform: "uppercase", letterSpacing: 1.2, marginBottom: 6, padding: "0 2px" }}>{cat}</div>
          {strats.map(s => (
            <div key={s.name} style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "8px 10px", marginBottom: 2, borderRadius: 6,
              background: s.active ? C.surfaceAlt : "transparent",
              border: `1px solid ${s.active ? (STRAT_COLORS[s.name] || C.border) + "30" : "transparent"}`,
              transition: "all 0.2s",
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <div style={{ width: 6, height: 6, borderRadius: 3, background: STRAT_COLORS[s.name] || C.textMuted, boxShadow: s.active ? `0 0 6px ${STRAT_COLORS[s.name] || C.textMuted}40` : "none" }} />
                  <span style={{ fontSize: 12, fontWeight: 600, color: s.active ? C.text : C.textMuted }}>{s.name}</span>
                  {s.active && <span style={{ fontSize: 10, color: C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>{(s.weight||1).toFixed(1)}x</span>}
                </div>
                <div style={{ fontSize: 10, color: C.textDim, marginTop: 1, marginLeft: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.description}</div>
              </div>
              <Toggle on={s.active} onChange={() => onToggle(s.name)} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
};

// ---- MARKET HEATMAP ----
const MarketHeatmap = ({ signals }) => {
  const items = signals.length > 0 ? signals : SIGNALS_DATA;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(80px, 1fr))", gap: 4 }}>
      {items.map(s => {
        const ticker = s.Ticker || s.ticker;
        const change = s.Change || s.change || 0;
        const up = change >= 0;
        return (
          <div key={ticker} style={{
            padding: "10px 6px", borderRadius: 6, textAlign: "center",
            background: up
              ? `rgba(38,166,154,${Math.min(0.35, Math.abs(change) * 0.07)})`
              : `rgba(239,83,80,${Math.min(0.35, Math.abs(change) * 0.07)})`,
            border: `1px solid ${up ? C.bullishDim : C.bearishDim}`,
            cursor: "pointer",
          }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: C.text, letterSpacing: 0.5, fontFamily: "'JetBrains Mono', monospace" }}>{ticker}</div>
            <div style={{ fontSize: 10, fontWeight: 600, color: up ? C.bullish : C.bearish, marginTop: 2, fontFamily: "'JetBrains Mono', monospace" }}>{fmtPct(change)}</div>
          </div>
        );
      })}
    </div>
  );
};

// ---- LLM PANEL ----
const LLMPanel = () => {
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("claude-sonnet-4-6");
  const [prompt, setPrompt] = useState("");
  const [response, setResponse] = useState("");
  const [loading, setLoading] = useState(false);

  const providers = {
    claude: { models: ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"], label: "Anthropic" },
    openai: { models: ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"], label: "OpenAI" },
    ollama: { models: ["llama3", "llama3.1", "mistral", "mixtral", "phi", "deepseek-coder"], label: "Ollama" },
    gemini: { models: ["gemini-1.5-pro", "gemini-pro"], label: "Gemini" },
    custom: { models: ["any"], label: "Custom" },
  };

  const analyze = async () => {
    if (!prompt.trim()) return;
    setLoading(true);
    const res = await apiPost("/api/llm/analyze", { prompt, ticker: null, include_portfolio: false });
    setResponse(res?.response || res?.error || "Configure an API key on the server to enable AI analysis.");
    setLoading(false);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", gap: 6 }}>
        {Object.entries(providers).map(([key, val]) => (
          <button key={key} onClick={() => { setProvider(key); setModel(val.models[0]); }}
            style={{
              padding: "8px 14px", borderRadius: 6, fontSize: 12, fontWeight: 600,
              border: `1px solid ${provider === key ? C.accent : C.border}`,
              background: provider === key ? C.accentDim : "transparent",
              color: provider === key ? C.accent : C.textMuted, cursor: "pointer",
            }}>{val.label}</button>
        ))}
      </div>
      <div>
        <label style={{ fontSize: 10, fontWeight: 600, color: C.textDim, textTransform: "uppercase", letterSpacing: 0.8, display: "block", marginBottom: 4 }}>Model</label>
        <select value={model} onChange={e => setModel(e.target.value)}
          style={{ width: "100%", padding: "8px 12px", borderRadius: 6, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, fontFamily: "'JetBrains Mono', monospace" }}>
          {providers[provider].models.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
      </div>
      <div>
        <label style={{ fontSize: 10, fontWeight: 600, color: C.textDim, textTransform: "uppercase", letterSpacing: 0.8, display: "block", marginBottom: 4 }}>Market Analysis Prompt</label>
        <div style={{ display: "flex", gap: 8 }}>
          <input value={prompt} onChange={e => setPrompt(e.target.value)}
            onKeyDown={e => e.key === "Enter" && analyze()}
            placeholder="Which pullback stocks have the best risk/reward?"
            style={{ flex: 1, padding: "9px 12px", borderRadius: 6, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, outline: "none" }} />
          <button onClick={analyze} disabled={loading} style={{
            padding: "9px 18px", borderRadius: 6, border: "none",
            background: C.accent, color: "#fff", fontSize: 12, fontWeight: 700,
            opacity: loading ? 0.5 : 1, display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
          }}>{loading ? <Spinner size={14}/> : Icons.brain(14)} Analyze</button>
        </div>
      </div>
      {response && (
        <div style={{ padding: 14, borderRadius: 6, background: C.surface, border: `1px solid ${C.border}`, fontSize: 12, color: C.textSoft, lineHeight: 1.7, whiteSpace: "pre-wrap" }}>{response}</div>
      )}
    </div>
  );
};

// ============================================================
// MAIN APP
// ============================================================
export default function App() {
  const [tab, setTab] = useState("dashboard");
  const [strategies, setStrategies] = useState([]);
  const [signals, setSignals] = useState([]);
  const [portfolio, setPortfolio] = useState(null);
  const [watchlist, setWatchlist] = useState([]);
  const [backtest, setBacktest] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [selectedTicker, setSelectedTicker] = useState("COIN");

  const equity = useRef(EQUITY).current;

  const loadData = useCallback(async () => {
    const [health, strats, sigs, port, wl] = await Promise.all([
      apiGet("/api/health"),
      apiGet("/api/strategies"),
      apiGet("/api/scan"),
      apiGet("/api/portfolio"),
      apiGet("/api/watchlist"),
    ]);
    setConnected(!!health);
    if (strats?.strategies) setStrategies(strats.strategies);
    if (sigs?.signals) setSignals(sigs.signals);
    if (port) setPortfolio(port);
    if (wl?.watchlist) setWatchlist(wl.watchlist);
    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);
  useEffect(() => { const id = setInterval(loadData, 30000); return () => clearInterval(id); }, [loadData]);

  const toggleStrategy = async (name) => {
    const s = strategies.find(x => x.name === name);
    if (!s) return;
    setStrategies(prev => prev.map(x => x.name === name ? { ...x, active: !x.active } : x));
    await apiPost("/api/strategies/activate", { name, active: !s.active });
  };

  const runScan = async () => {
    setScanning(true);
    const res = await apiGet("/api/scan");
    if (res?.signals) setSignals(res.signals);
    setScanning(false);
  };

  const runBacktest = async () => {
    const res = await apiGet("/api/backtest?capital=100000&days=504");
    if (res) setBacktest(res);
  };

  const strats = strategies.length > 0 ? strategies : STRATEGIES_DATA;
  const sigs = signals.length > 0 ? signals : SIGNALS_DATA;
  const activeCount = strats.filter(s => s.active).length;
  const buySignals = sigs.filter(s => (s.Signal || s.signal || "").includes("BUY")).length;
  const strongBuys = sigs.filter(s => (s.Signal || s.signal) === "STRONG_BUY").length;
  const holdSignals = sigs.length - buySignals;

  const tabs = [
    { id: "dashboard", label: "Overview", icon: Icons.chart },
    { id: "signals", label: "Signals", icon: Icons.zap },
    { id: "trade", label: "Trade", icon: Icons.wallet },
    { id: "backtest", label: "Backtest", icon: Icons.activity },
    { id: "ai", label: "AI Lab", icon: Icons.brain },
  ];

  if (loading) {
    return (
      <div style={{ height: "100vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: C.bg, gap: 20 }}>
        <div style={{ width: 52, height: 52, borderRadius: 14, background: `linear-gradient(135deg, ${C.accent}, ${C.purple})`, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: `0 0 40px ${C.accent}30` }}>
          <span style={{ fontWeight: 800, fontSize: 24, color: "#fff", fontFamily: "'JetBrains Mono', monospace" }}>P</span>
        </div>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, textAlign: "center", letterSpacing: -0.5 }}>ProxiAlpha</div>
          <div style={{ fontSize: 12, color: C.textDim, textAlign: "center", marginTop: 4, letterSpacing: 0.5 }}>CONNECTING TO TRADING ENGINE</div>
        </div>
        <Spinner size={20} />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", background: C.bg, color: C.text, fontFamily: "'DM Sans', system-ui, sans-serif", overflow: "hidden" }}>
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

        {/* SIDEBAR */}
        <div style={{
          width: sidebarOpen ? 280 : 0, overflow: "hidden", transition: "width 0.25s cubic-bezier(0.4, 0, 0.2, 1)",
          background: C.surface, borderRight: `1px solid ${C.border}`, display: "flex", flexDirection: "column", flexShrink: 0,
        }}>
          <div style={{ padding: "14px 16px", borderBottom: `1px solid ${C.border}` }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ width: 32, height: 32, borderRadius: 8, background: `linear-gradient(135deg, ${C.accent}, ${C.purple})`, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: `0 0 16px ${C.accent}20` }}>
                <span style={{ fontWeight: 800, fontSize: 14, color: "#fff", fontFamily: "'JetBrains Mono', monospace" }}>P</span>
              </div>
              <div>
                <div style={{ fontWeight: 700, fontSize: 14, letterSpacing: -0.3 }}>ProxiAlpha</div>
                <div style={{ fontSize: 10, color: C.textDim, letterSpacing: 0.5, fontFamily: "'JetBrains Mono', monospace" }}>{activeCount} STRATEGIES ACTIVE</div>
              </div>
            </div>
          </div>
          <div style={{ padding: "10px 12px", overflowY: "auto", flex: 1 }}>
            <StrategyPanel strategies={strats} onToggle={toggleStrategy} />
          </div>
          <div style={{ padding: "10px 14px", borderTop: `1px solid ${C.border}` }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 11 }}>
              <span style={{ color: C.textDim }}>{activeCount}/{strats.length}</span>
              <span style={{ display: "flex", alignItems: "center", gap: 4, color: connected ? C.bullish : C.bearish }}>
                <span style={{ width: 6, height: 6, borderRadius: 3, background: connected ? C.bullish : C.bearish }} />
                <span style={{ fontSize: 10, letterSpacing: 0.5, fontFamily: "'JetBrains Mono', monospace" }}>{connected ? "LIVE" : "OFFLINE"}</span>
              </span>
            </div>
          </div>
        </div>

        {/* MAIN AREA */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {/* TOP NAV */}
          <div style={{ height: 48, display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 16px", borderBottom: `1px solid ${C.border}`, background: C.surface, flexShrink: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <button onClick={() => setSidebarOpen(!sidebarOpen)} style={{ background: "none", border: "none", color: C.textMuted, padding: 4, display: "flex", cursor: "pointer" }}>{Icons.panelLeft(18)}</button>
              <div style={{ width: 1, height: 24, background: C.border }} />
              <div style={{ display: "flex", gap: 2 }}>
                {tabs.map(t => (
                  <button key={t.id} onClick={() => setTab(t.id)} style={{
                    padding: "6px 14px", borderRadius: 6, border: "none",
                    background: tab === t.id ? C.accentDim : "transparent",
                    color: tab === t.id ? C.accent : C.textMuted,
                    fontSize: 12, fontWeight: tab === t.id ? 600 : 500,
                    display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
                  }}>
                    <span style={{ opacity: tab === t.id ? 1 : 0.6 }}>{t.icon(14)}</span>
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ padding: "4px 10px", borderRadius: 4, background: C.bullishDim, border: `1px solid ${C.bullish}20`, color: C.bullish, fontSize: 10, fontWeight: 700, letterSpacing: 1, fontFamily: "'JetBrains Mono', monospace" }}>PAPER</div>
            </div>
          </div>

          {/* CONTENT */}
          <div style={{ flex: 1, overflow: "auto", padding: 16 }}>

            {/* DASHBOARD */}
            {tab === "dashboard" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 1400 }}>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <MetricCard label="Portfolio Value" value={fmtK((portfolio?.state?.cash || 100000))} color={C.bullish} trend={17.1} icon={Icons.wallet(14)} />
                  <MetricCard label="Positions" value={Object.keys(portfolio?.state?.positions || {}).length} sub={`of ${sigs.length} tracked`} icon={Icons.activity(14)} />
                  <MetricCard label="Buy Signals" value={buySignals} sub={`${strongBuys} strong buys`} color={C.bullish} icon={Icons.zap(14)} />
                  <MetricCard label="Active Strategies" value={activeCount} sub={`of ${strats.length} total`} color={C.accent} icon={Icons.settings(14)} />
                  <MetricCard label="Sharpe Ratio" value="1.92" sub="Annualized" color={C.accent} icon={Icons.target(14)} />
                  <MetricCard label="Max Drawdown" value="-6.8%" sub="Peak to trough" color={C.bearish} icon={Icons.shield(14)} />
                </div>

                {/* Market Heatmap */}
                <div style={{ background: C.card, borderRadius: 8, padding: 16, border: `1px solid ${C.border}` }}>
                  <SectionHeader title="Market Heatmap" subtitle="Daily performance across watchlist" icon={Icons.chart(14)} />
                  <MarketHeatmap signals={sigs} />
                </div>

                {/* Charts Row */}
                <div style={{ display: "grid", gridTemplateColumns: "3fr 2fr", gap: 12 }}>
                  <div style={{ background: C.card, borderRadius: 8, padding: 16, border: `1px solid ${C.border}` }}>
                    <SectionHeader title="Portfolio vs Benchmark" subtitle="180-day equity curve" icon={Icons.chart(14)} />
                    <ResponsiveContainer width="100%" height={260}>
                      <AreaChart data={equity}>
                        <defs>
                          <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor={C.accent} stopOpacity={0.2} />
                            <stop offset="100%" stopColor={C.accent} stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                        <XAxis dataKey="day" tick={{ fill: C.textDim, fontSize: 10 }} axisLine={{ stroke: C.border }} tickLine={false} />
                        <YAxis tick={{ fill: C.textDim, fontSize: 10 }} axisLine={{ stroke: C.border }} tickLine={false} tickFormatter={v => `$${(v/1000).toFixed(0)}k`} />
                        <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, color: C.text, fontSize: 12 }} formatter={v => [`$${v.toLocaleString()}`, ""]} />
                        <Area type="monotone" dataKey="portfolio" stroke={C.accent} strokeWidth={2} fill="url(#eqGrad)" name="Portfolio" />
                        <Line type="monotone" dataKey="benchmark" stroke={C.textDim} strokeWidth={1} strokeDasharray="4 4" dot={false} name="S&P 500" />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                  <div style={{ background: C.card, borderRadius: 8, padding: 16, border: `1px solid ${C.border}` }}>
                    <SectionHeader title="Signal Distribution" icon={Icons.target(14)} />
                    <ResponsiveContainer width="100%" height={260}>
                      <PieChart>
                        <Pie data={[
                          { name: "Strong Buy", value: strongBuys || 1 },
                          { name: "Buy", value: (buySignals - strongBuys) || 1 },
                          { name: "Hold", value: holdSignals || 1 },
                        ]} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={90} innerRadius={55} strokeWidth={0}
                          label={({ name, percent }) => `${name} ${(percent*100).toFixed(0)}%`} labelLine={{ stroke: C.textDim }}>
                          <Cell fill={C.bullish} />
                          <Cell fill="#5ec4b6" />
                          <Cell fill={C.textDim} />
                        </Pie>
                        <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, fontSize: 12 }} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </div>
            )}

            {/* SIGNALS */}
            {tab === "signals" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 1400 }}>
                <SectionHeader title="Trading Signals" subtitle={`${activeCount} strategies contributing`} icon={Icons.zap(16)}
                  action={
                    <button onClick={runScan} disabled={scanning} style={{
                      padding: "6px 14px", borderRadius: 6, fontSize: 11, fontWeight: 700,
                      border: `1px solid ${C.accent}50`, background: C.accentDim, color: C.accent,
                      display: "flex", alignItems: "center", gap: 6, opacity: scanning ? 0.5 : 1,
                      fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5, cursor: "pointer",
                    }}>{scanning ? <Spinner size={12}/> : Icons.refresh(12)} SCAN</button>
                  } />
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <MetricCard label="Strong Buys" value={strongBuys} color={C.bullish} icon={Icons.zap(14)} />
                  <MetricCard label="Buy Signals" value={buySignals - strongBuys} color="#5ec4b6" />
                  <MetricCard label="Hold" value={holdSignals} color={C.textMuted} />
                  <MetricCard label="Avg Confidence" value={sigs.length ? `${(sigs.reduce((a, s) => a + (s.Confidence || s.confidence || 0), 0) / sigs.length * 100).toFixed(0)}%` : "\u2014"} color={C.accent} icon={Icons.target(14)} />
                </div>
                <div style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, overflow: "hidden" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                        {["Symbol", "Signal", "Score", "Confidence", "Price", "Change", "Target", "Stop"].map(h => (
                          <th key={h} style={{ padding: "10px 14px", textAlign: "left", color: C.textDim, fontWeight: 600, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.8, background: C.surfaceAlt }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {sigs.map((s, i) => {
                        const ticker = s.Ticker || s.ticker;
                        const signal = s.Signal || s.signal;
                        const score = s.Score || s.score || 0;
                        const conf = s.Confidence || s.confidence || 0;
                        const target = s.Target || s.target;
                        const stop = s.Stop || s.stop_loss;
                        const price = s.Price || s.price || 0;
                        const change = s.Change || s.change || 0;
                        return (
                          <tr key={ticker + i} style={{ borderBottom: `1px solid ${C.border}`, transition: "background 0.15s", cursor: "pointer" }}
                            onClick={() => { setSelectedTicker(ticker); setTab("dashboard"); }}>
                            <td style={{ padding: "10px 14px", fontWeight: 700, color: C.accent, fontSize: 12, letterSpacing: 0.5, fontFamily: "'JetBrains Mono', monospace" }}>{ticker}</td>
                            <td style={{ padding: "10px 14px" }}><SignalBadge signal={signal} /></td>
                            <td style={{ padding: "10px 14px", fontSize: 12, fontWeight: 600, color: score > 0.8 ? C.bullish : score > 0.3 ? C.textSoft : C.textMuted, fontFamily: "'JetBrains Mono', monospace" }}>{score > 0 ? "+" : ""}{score.toFixed(2)}</td>
                            <td style={{ padding: "10px 14px" }}><ConfidenceBar value={conf} /></td>
                            <td style={{ padding: "10px 14px", fontSize: 12, color: C.textSoft, fontFamily: "'JetBrains Mono', monospace" }}>${fmt(price)}</td>
                            <td style={{ padding: "10px 14px", fontSize: 12, fontWeight: 600, color: change >= 0 ? C.bullish : C.bearish, fontFamily: "'JetBrains Mono', monospace" }}>
                              <span style={{ display: "flex", alignItems: "center", gap: 3 }}>
                                {change >= 0 ? Icons.trendUp(10) : Icons.trendDown(10)}
                                {fmtPct(change)}
                              </span>
                            </td>
                            <td style={{ padding: "10px 14px", fontSize: 12, color: target ? C.bullish : C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>{target ? `$${fmt(target, 0)}` : "\u2014"}</td>
                            <td style={{ padding: "10px 14px", fontSize: 12, color: stop ? C.bearish : C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>{stop ? `$${fmt(stop, 0)}` : "\u2014"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* TRADE */}
            {tab === "trade" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 1200 }}>
                <SectionHeader title="Paper Trading" subtitle="Execute against your paper portfolio" icon={Icons.wallet(16)} />
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <MetricCard label="Cash Balance" value={fmtK((portfolio?.state?.cash ?? 100000))} color={C.bullish} icon={Icons.wallet(14)} />
                  <MetricCard label="Positions" value={Object.keys(portfolio?.state?.positions || {}).length} sub="open" icon={Icons.activity(14)} />
                  <MetricCard label="Total Trades" value={portfolio?.state?.trades?.length || 0} icon={Icons.zap(14)} />
                </div>
              </div>
            )}

            {/* BACKTEST */}
            {tab === "backtest" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 1400 }}>
                <SectionHeader title="Backtesting Engine" subtitle="Historical simulation with current strategy weights" icon={Icons.activity(16)}
                  action={
                    <button onClick={runBacktest} style={{
                      padding: "6px 14px", borderRadius: 6, fontSize: 11, fontWeight: 700,
                      border: `1px solid ${C.purple}50`, background: C.purpleDim, color: C.purple,
                      display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
                      fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5,
                    }}>{Icons.play(12)} RUN</button>
                  } />
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <MetricCard label="Total Trades" value={backtest?.summary?.total_trades ?? "\u2014"} icon={Icons.activity(14)} />
                  <MetricCard label="Win Rate" value={backtest?.summary?.win_rate != null ? `${(backtest.summary.win_rate*100).toFixed(0)}%` : "\u2014"} color={C.bullish} icon={Icons.target(14)} />
                  <MetricCard label="Sharpe Ratio" value={backtest?.summary?.sharpe_ratio?.toFixed(2) ?? "\u2014"} color={C.accent} />
                  <MetricCard label="Max Drawdown" value={backtest?.summary?.max_drawdown != null ? `${(backtest.summary.max_drawdown*100).toFixed(1)}%` : "\u2014"} color={C.bearish} icon={Icons.shield(14)} />
                </div>
                <div style={{ background: C.card, borderRadius: 8, padding: 16, border: `1px solid ${C.border}` }}>
                  <SectionHeader title="Strategy Equity Curve" icon={Icons.chart(14)} />
                  <ResponsiveContainer width="100%" height={320}>
                    <ComposedChart data={equity}>
                      <defs>
                        <linearGradient id="btGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor={C.bullish} stopOpacity={0.15} />
                          <stop offset="100%" stopColor={C.bullish} stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                      <XAxis dataKey="day" tick={{ fill: C.textDim, fontSize: 10 }} axisLine={{ stroke: C.border }} tickLine={false} />
                      <YAxis tick={{ fill: C.textDim, fontSize: 10 }} axisLine={{ stroke: C.border }} tickLine={false} tickFormatter={v => `$${(v/1000).toFixed(0)}k`} />
                      <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, color: C.text, fontSize: 12 }} formatter={v => [`$${v.toLocaleString()}`, ""]} />
                      <Area type="monotone" dataKey="portfolio" stroke={C.bullish} strokeWidth={2} fill="url(#btGrad)" name="Strategy" />
                      <Line type="monotone" dataKey="benchmark" stroke={C.textDim} strokeWidth={1} strokeDasharray="4 4" dot={false} name="Benchmark" />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* AI LAB */}
            {tab === "ai" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 1400 }}>
                <div style={{ background: `linear-gradient(135deg, ${C.accentDim}, ${C.purpleDim})`, borderRadius: 8, padding: 22, border: `1px solid ${C.accent}20`, position: "relative", overflow: "hidden" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                    <span style={{ color: C.accent }}>{Icons.brain(20)}</span>
                    <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: -0.3 }}>AI Integration Lab</h2>
                  </div>
                  <p style={{ color: C.textSoft, margin: 0, fontSize: 13, lineHeight: 1.7, maxWidth: 700 }}>
                    Connect any LLM provider for signal generation, parameter optimization, and market analysis.
                  </p>
                </div>
                <div style={{ background: C.card, borderRadius: 8, padding: 18, border: `1px solid ${C.border}` }}>
                  <SectionHeader title="LLM Configuration" icon={Icons.settings(14)} />
                  <LLMPanel />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

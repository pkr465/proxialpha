import { useState, useEffect, useCallback } from "react";
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, PieChart, Pie, Cell, RadarChart,
  Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis
} from "recharts";

// ============================================================
// PROXIALPHA - REACT DASHBOARD
// Interactive charts, real-time signals, portfolio tracking
// ============================================================

const COLORS = {
  positive: "#22c55e", negative: "#ef4444", neutral: "#6b7280",
  primary: "#3b82f6", secondary: "#a855f7", warning: "#f59e0b",
  bg: "#111827", card: "#1f2937", text: "#f9fafb", muted: "#9ca3af",
};

const SECTOR_COLORS = [
  "#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7",
  "#06b6d4", "#ec4899", "#84cc16",
];

// --- SAMPLE DATA (replace with live API data) ---
const WATCHLIST_DATA = [
  { ticker: "COIN", sector: "Crypto/Fintech", ath: 443, low: 138, current: 175, drawdown: -60.5, recovery: 12.1, rsi: 38, signal: "BUY" },
  { ticker: "HOOD", sector: "Crypto/Fintech", ath: 153, low: 63, current: 72, drawdown: -52.9, recovery: 10.0, rsi: 42, signal: "HOLD" },
  { ticker: "ORCL", sector: "Tech", ath: 345, low: 136, current: 155, drawdown: -55.1, recovery: 9.1, rsi: 35, signal: "BUY" },
  { ticker: "MSTR", sector: "Crypto/Tech", ath: 543, low: 105, current: 185, drawdown: -65.9, recovery: 18.3, rsi: 44, signal: "STRONG_BUY" },
  { ticker: "ETH", sector: "Crypto", ath: 4956, low: 1795, current: 2100, drawdown: -57.6, recovery: 9.7, rsi: 41, signal: "BUY" },
  { ticker: "NOW", sector: "Tech/SaaS", ath: 239, low: 98, current: 115, drawdown: -51.9, recovery: 12.1, rsi: 39, signal: "BUY" },
  { ticker: "SOFI", sector: "Fintech", ath: 32, low: 15, current: 18, drawdown: -43.8, recovery: 17.6, rsi: 45, signal: "HOLD" },
  { ticker: "HIMS", sector: "Healthcare", ath: 72, low: 13, current: 22, drawdown: -69.4, recovery: 15.3, rsi: 33, signal: "STRONG_BUY" },
  { ticker: "NKE", sector: "Consumer", ath: 179, low: 44, current: 58, drawdown: -67.6, recovery: 10.4, rsi: 31, signal: "BUY" },
  { ticker: "NVO", sector: "Healthcare", ath: 148, low: 36, current: 52, drawdown: -64.9, recovery: 14.3, rsi: 36, signal: "BUY" },
  { ticker: "UNH", sector: "Healthcare", ath: 632, low: 234, current: 310, drawdown: -51.0, recovery: 19.1, rsi: 40, signal: "BUY" },
  { ticker: "IREN", sector: "Crypto/Mining", ath: 76, low: 30, current: 38, drawdown: -50.0, recovery: 17.4, rsi: 43, signal: "HOLD" },
  { ticker: "TGT", sector: "Consumer", ath: 269, low: 83, current: 102, drawdown: -62.1, recovery: 10.2, rsi: 37, signal: "BUY" },
  { ticker: "EL", sector: "Consumer", ath: 373, low: 69, current: 85, drawdown: -77.2, recovery: 5.3, rsi: 28, signal: "STRONG_BUY" },
  { ticker: "LULU", sector: "Consumer", ath: 516, low: 143, current: 180, drawdown: -65.1, recovery: 9.9, rsi: 34, signal: "BUY" },
];

const EQUITY_CURVE = Array.from({ length: 90 }, (_, i) => ({
  day: i + 1,
  portfolio: 100000 + Math.sin(i / 10) * 5000 + i * 80 + (Math.random() - 0.3) * 2000,
  spy: 100000 + i * 50 + (Math.random() - 0.5) * 1000,
}));

const MONTHLY_RETURNS = [
  { month: "Jan", return: 4.2 }, { month: "Feb", return: -1.8 },
  { month: "Mar", return: 6.5 }, { month: "Apr", return: 2.1 },
  { month: "May", return: -3.2 }, { month: "Jun", return: 5.8 },
  { month: "Jul", return: 1.4 }, { month: "Aug", return: -0.9 },
  { month: "Sep", return: 3.7 }, { month: "Oct", return: 7.2 },
  { month: "Nov", return: -2.1 }, { month: "Dec", return: 4.6 },
];

// --- COMPONENTS ---
const MetricCard = ({ title, value, subtitle, color }) => (
  <div style={{ background: COLORS.card, borderRadius: 12, padding: "16px 20px", flex: 1, minWidth: 160 }}>
    <div style={{ color: COLORS.muted, fontSize: 12, marginBottom: 4 }}>{title}</div>
    <div style={{ color: color || COLORS.text, fontSize: 24, fontWeight: 700 }}>{value}</div>
    {subtitle && <div style={{ color: COLORS.muted, fontSize: 11, marginTop: 2 }}>{subtitle}</div>}
  </div>
);

const SignalBadge = ({ signal }) => {
  const colors = {
    STRONG_BUY: "#22c55e", BUY: "#86efac", HOLD: "#6b7280",
    SELL: "#fca5a5", STRONG_SELL: "#ef4444",
  };
  return (
    <span style={{
      background: colors[signal] || COLORS.muted, color: "#000",
      padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 600,
    }}>{signal}</span>
  );
};

const WatchlistTable = ({ data, onSelect }) => (
  <div style={{ overflowX: "auto" }}>
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: `1px solid ${COLORS.muted}33` }}>
          {["Ticker", "Sector", "ATH", "Low", "Current", "Drawdown", "Recovery", "RSI", "Signal"].map(h => (
            <th key={h} style={{ padding: "8px 12px", textAlign: "left", color: COLORS.muted, fontWeight: 500 }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map(stock => (
          <tr key={stock.ticker}
            onClick={() => onSelect?.(stock.ticker)}
            style={{ borderBottom: `1px solid ${COLORS.muted}15`, cursor: "pointer" }}
            onMouseEnter={e => e.currentTarget.style.background = COLORS.card}
            onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
            <td style={{ padding: "8px 12px", fontWeight: 600, color: COLORS.primary }}>{stock.ticker}</td>
            <td style={{ padding: "8px 12px", color: COLORS.muted }}>{stock.sector}</td>
            <td style={{ padding: "8px 12px" }}>${stock.ath}</td>
            <td style={{ padding: "8px 12px" }}>${stock.low}</td>
            <td style={{ padding: "8px 12px", fontWeight: 600 }}>${stock.current}</td>
            <td style={{ padding: "8px 12px", color: COLORS.negative }}>{stock.drawdown.toFixed(1)}%</td>
            <td style={{ padding: "8px 12px", color: COLORS.positive }}>{stock.recovery.toFixed(1)}%</td>
            <td style={{ padding: "8px 12px", color: stock.rsi < 30 ? COLORS.positive : stock.rsi > 70 ? COLORS.negative : COLORS.text }}>
              {stock.rsi}
            </td>
            <td style={{ padding: "8px 12px" }}><SignalBadge signal={stock.signal} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

const DrawdownChart = ({ data }) => (
  <ResponsiveContainer width="100%" height={300}>
    <BarChart data={data.sort((a, b) => a.drawdown - b.drawdown)} layout="vertical">
      <CartesianGrid strokeDasharray="3 3" stroke={COLORS.muted + "22"} />
      <XAxis type="number" domain={[-100, 0]} tick={{ fill: COLORS.muted, fontSize: 11 }} />
      <YAxis type="category" dataKey="ticker" tick={{ fill: COLORS.text, fontSize: 11 }} width={50} />
      <Tooltip contentStyle={{ background: COLORS.card, border: "none", borderRadius: 8, color: COLORS.text }} />
      <Bar dataKey="drawdown" fill={COLORS.negative} radius={[0, 4, 4, 0]} name="Drawdown %">
        {data.map((_, i) => <Cell key={i} fill={`hsl(${Math.abs(data[i]?.drawdown || 0) * 1.2}, 80%, 50%)`} />)}
      </Bar>
    </BarChart>
  </ResponsiveContainer>
);

const SectorPie = ({ data }) => {
  const sectors = {};
  data.forEach(s => { sectors[s.sector] = (sectors[s.sector] || 0) + 1; });
  const pieData = Object.entries(sectors).map(([name, value]) => ({ name, value }));

  return (
    <ResponsiveContainer width="100%" height={250}>
      <PieChart>
        <Pie data={pieData} dataKey="value" nameKey="name" cx="50%" cy="50%"
          outerRadius={90} label={({ name, percent }) => `${name} (${(percent * 100).toFixed(0)}%)`}
          labelLine={{ stroke: COLORS.muted }}>
          {pieData.map((_, i) => <Cell key={i} fill={SECTOR_COLORS[i % SECTOR_COLORS.length]} />)}
        </Pie>
        <Tooltip />
      </PieChart>
    </ResponsiveContainer>
  );
};

// --- MAIN APP ---
export default function App() {
  const [activeTab, setActiveTab] = useState("overview");
  const [selectedStock, setSelectedStock] = useState(null);

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "watchlist", label: "Watchlist" },
    { id: "backtest", label: "Backtest" },
    { id: "ai", label: "AI Panel" },
  ];

  const buySignals = WATCHLIST_DATA.filter(s => s.signal.includes("BUY")).length;
  const avgDrawdown = (WATCHLIST_DATA.reduce((a, s) => a + s.drawdown, 0) / WATCHLIST_DATA.length).toFixed(1);
  const deepestPullback = WATCHLIST_DATA.reduce((a, s) => s.drawdown < a.drawdown ? s : a);

  return (
    <div style={{ background: COLORS.bg, minHeight: "100vh", color: COLORS.text, fontFamily: "system-ui, -apple-system, sans-serif" }}>
      {/* Header */}
      <div style={{ background: COLORS.card, padding: "16px 24px", display: "flex", alignItems: "center", justifyContent: "space-between", borderBottom: `1px solid ${COLORS.muted}22` }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ width: 32, height: 32, borderRadius: 8, background: `linear-gradient(135deg, ${COLORS.primary}, ${COLORS.secondary})`, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, fontSize: 16 }}>P</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 18 }}>ProxiAlpha</div>
            <div style={{ color: COLORS.muted, fontSize: 11 }}>proxiant.ai/proxialpha</div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {tabs.map(tab => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id)}
              style={{
                padding: "6px 16px", borderRadius: 6, border: "none", cursor: "pointer",
                background: activeTab === tab.id ? COLORS.primary : "transparent",
                color: activeTab === tab.id ? "#fff" : COLORS.muted,
                fontSize: 13, fontWeight: 500,
              }}>
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{ padding: 24, maxWidth: 1400, margin: "0 auto" }}>
        {/* OVERVIEW TAB */}
        {activeTab === "overview" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            {/* Metric Cards */}
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <MetricCard title="Portfolio Value" value="$112,480" subtitle="+12.5% all time" color={COLORS.positive} />
              <MetricCard title="Today's P&L" value="+$840" subtitle="+0.75%" color={COLORS.positive} />
              <MetricCard title="Open Positions" value="6" subtitle="of 15 watchlist" />
              <MetricCard title="Buy Signals" value={buySignals} subtitle="Active opportunities" color={COLORS.primary} />
              <MetricCard title="Win Rate" value="68%" subtitle="Last 50 trades" color={COLORS.positive} />
              <MetricCard title="Sharpe Ratio" value="1.82" subtitle="Annualized" color={COLORS.primary} />
            </div>

            {/* Equity Curve */}
            <div style={{ background: COLORS.card, borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Portfolio vs S&P 500</h3>
              <ResponsiveContainer width="100%" height={350}>
                <AreaChart data={EQUITY_CURVE}>
                  <defs>
                    <linearGradient id="portfolioGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={COLORS.primary} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={COLORS.primary} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={COLORS.muted + "22"} />
                  <XAxis dataKey="day" tick={{ fill: COLORS.muted, fontSize: 11 }} />
                  <YAxis tick={{ fill: COLORS.muted, fontSize: 11 }} domain={["auto", "auto"]} />
                  <Tooltip contentStyle={{ background: COLORS.card, border: "none", borderRadius: 8, color: COLORS.text }} formatter={v => [`$${v.toFixed(0)}`, ""]} />
                  <Area type="monotone" dataKey="portfolio" stroke={COLORS.primary} fill="url(#portfolioGrad)" strokeWidth={2} name="Portfolio" />
                  <Line type="monotone" dataKey="spy" stroke={COLORS.muted} strokeWidth={1} strokeDasharray="5 5" dot={false} name="S&P 500" />
                  <Legend />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Monthly Returns */}
            <div style={{ background: COLORS.card, borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Monthly Returns</h3>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={MONTHLY_RETURNS}>
                  <CartesianGrid strokeDasharray="3 3" stroke={COLORS.muted + "22"} />
                  <XAxis dataKey="month" tick={{ fill: COLORS.muted, fontSize: 11 }} />
                  <YAxis tick={{ fill: COLORS.muted, fontSize: 11 }} />
                  <Tooltip contentStyle={{ background: COLORS.card, border: "none", borderRadius: 8, color: COLORS.text }} />
                  <Bar dataKey="return" name="Return %">
                    {MONTHLY_RETURNS.map((entry, i) => (
                      <Cell key={i} fill={entry.return >= 0 ? COLORS.positive : COLORS.negative} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* WATCHLIST TAB */}
        {activeTab === "watchlist" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <MetricCard title="Avg Drawdown" value={`${avgDrawdown}%`} color={COLORS.negative} />
              <MetricCard title="Deepest Pullback" value={`${deepestPullback.ticker}`} subtitle={`${deepestPullback.drawdown.toFixed(1)}% from ATH`} color={COLORS.negative} />
              <MetricCard title="Stocks Tracked" value={WATCHLIST_DATA.length} />
              <MetricCard title="Buy Signals" value={buySignals} color={COLORS.positive} />
            </div>

            <div style={{ background: COLORS.card, borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Pullback Watchlist</h3>
              <WatchlistTable data={WATCHLIST_DATA} onSelect={setSelectedStock} />
            </div>

            <div style={{ display: "flex", gap: 20 }}>
              <div style={{ background: COLORS.card, borderRadius: 12, padding: 20, flex: 2 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Drawdown Depth</h3>
                <DrawdownChart data={WATCHLIST_DATA} />
              </div>
              <div style={{ background: COLORS.card, borderRadius: 12, padding: 20, flex: 1 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Sector Distribution</h3>
                <SectorPie data={WATCHLIST_DATA} />
              </div>
            </div>
          </div>
        )}

        {/* BACKTEST TAB */}
        {activeTab === "backtest" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <MetricCard title="Total Return" value="+27.4%" color={COLORS.positive} />
              <MetricCard title="Sharpe Ratio" value="1.82" color={COLORS.primary} />
              <MetricCard title="Max Drawdown" value="-8.3%" color={COLORS.negative} />
              <MetricCard title="Win Rate" value="68%" color={COLORS.positive} />
              <MetricCard title="Profit Factor" value="2.15" color={COLORS.primary} />
              <MetricCard title="Total Trades" value="142" />
            </div>

            <div style={{ background: COLORS.card, borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Backtest Equity Curve (3 Years)</h3>
              <ResponsiveContainer width="100%" height={350}>
                <AreaChart data={EQUITY_CURVE}>
                  <defs>
                    <linearGradient id="btGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={COLORS.positive} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={COLORS.positive} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={COLORS.muted + "22"} />
                  <XAxis dataKey="day" tick={{ fill: COLORS.muted, fontSize: 11 }} />
                  <YAxis tick={{ fill: COLORS.muted, fontSize: 11 }} domain={["auto", "auto"]} />
                  <Tooltip contentStyle={{ background: COLORS.card, border: "none", borderRadius: 8 , color: COLORS.text}} />
                  <Area type="monotone" dataKey="portfolio" stroke={COLORS.positive} fill="url(#btGrad)" strokeWidth={2} name="Strategy" />
                  <Line type="monotone" dataKey="spy" stroke={COLORS.muted} strokeWidth={1} strokeDasharray="5 5" dot={false} name="Benchmark (SPY)" />
                  <Legend />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            <div style={{ background: COLORS.card, borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Strategy Performance Radar</h3>
              <ResponsiveContainer width="100%" height={300}>
                <RadarChart data={[
                  { metric: "Return", DipBuyer: 85, Technical: 70, DCA: 60, AI: 90 },
                  { metric: "Sharpe", DipBuyer: 75, Technical: 80, DCA: 65, AI: 85 },
                  { metric: "Win Rate", DipBuyer: 70, Technical: 65, DCA: 80, AI: 75 },
                  { metric: "Max DD", DipBuyer: 60, Technical: 70, DCA: 85, AI: 65 },
                  { metric: "Consistency", DipBuyer: 65, Technical: 75, DCA: 90, AI: 70 },
                ]}>
                  <PolarGrid stroke={COLORS.muted + "33"} />
                  <PolarAngleAxis dataKey="metric" tick={{ fill: COLORS.muted, fontSize: 11 }} />
                  <PolarRadiusAxis tick={false} domain={[0, 100]} />
                  <Radar name="DipBuyer" dataKey="DipBuyer" stroke={COLORS.primary} fill={COLORS.primary} fillOpacity={0.15} />
                  <Radar name="Technical" dataKey="Technical" stroke={COLORS.secondary} fill={COLORS.secondary} fillOpacity={0.15} />
                  <Radar name="DCA" dataKey="DCA" stroke={COLORS.positive} fill={COLORS.positive} fillOpacity={0.15} />
                  <Radar name="AI" dataKey="AI" stroke={COLORS.warning} fill={COLORS.warning} fillOpacity={0.15} />
                  <Legend />
                </RadarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* AI PANEL TAB */}
        {activeTab === "ai" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <div style={{ background: `linear-gradient(135deg, ${COLORS.primary}22, ${COLORS.secondary}22)`, borderRadius: 12, padding: 24, border: `1px solid ${COLORS.primary}33` }}>
              <h3 style={{ margin: "0 0 8px", fontSize: 18, fontWeight: 700 }}>Claude AI Integration</h3>
              <p style={{ color: COLORS.muted, margin: 0, fontSize: 14, lineHeight: 1.6 }}>
                Connect your Anthropic API key to enable AI-powered signal generation,
                strategy optimization, and real-time market analysis. Claude analyzes
                technical indicators, pullback depth, volume patterns, and sector momentum
                to generate high-conviction trading signals.
              </p>
            </div>

            <div style={{ display: "flex", gap: 20 }}>
              <div style={{ background: COLORS.card, borderRadius: 12, padding: 20, flex: 1 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>AI Integration Modes</h3>
                {[
                  { name: "Signal Generation", desc: "AI analyzes each stock and generates BUY/SELL/HOLD signals", status: "Configure API Key" },
                  { name: "Strategy Optimization", desc: "AI reviews backtest results and tunes parameters", status: "Configure API Key" },
                  { name: "Risk Monitor", desc: "AI monitors portfolio for risk events and concentration", status: "Configure API Key" },
                  { name: "Market Analysis", desc: "AI generates daily market commentary and sector analysis", status: "Configure API Key" },
                ].map(mode => (
                  <div key={mode.name} style={{ padding: "12px 0", borderBottom: `1px solid ${COLORS.muted}15` }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <div>
                        <div style={{ fontWeight: 600, fontSize: 14 }}>{mode.name}</div>
                        <div style={{ color: COLORS.muted, fontSize: 12 }}>{mode.desc}</div>
                      </div>
                      <span style={{ background: COLORS.warning + "22", color: COLORS.warning, padding: "2px 8px", borderRadius: 4, fontSize: 11 }}>
                        {mode.status}
                      </span>
                    </div>
                  </div>
                ))}
              </div>

              <div style={{ background: COLORS.card, borderRadius: 12, padding: 20, flex: 1 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Strategy Weights</h3>
                {[
                  { name: "Dip Buyer", weight: 1.2, color: COLORS.primary },
                  { name: "Technical", weight: 1.0, color: COLORS.secondary },
                  { name: "DCA", weight: 0.8, color: COLORS.positive },
                  { name: "Custom Rules", weight: 0.9, color: COLORS.warning },
                  { name: "AI/Claude", weight: 1.5, color: "#ec4899" },
                ].map(s => (
                  <div key={s.name} style={{ marginBottom: 12 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                      <span>{s.name}</span>
                      <span style={{ color: COLORS.muted }}>{s.weight}x</span>
                    </div>
                    <div style={{ background: COLORS.bg, borderRadius: 4, height: 8 }}>
                      <div style={{ background: s.color, borderRadius: 4, height: 8, width: `${(s.weight / 1.5) * 100}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ background: COLORS.card, borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>How to Connect Claude</h3>
              <div style={{ fontFamily: "monospace", fontSize: 13, background: COLORS.bg, padding: 16, borderRadius: 8, lineHeight: 1.8 }}>
                <div style={{ color: COLORS.muted }}># 1. Set your API key in config_ai_integration.yaml</div>
                <div style={{ color: COLORS.positive }}>anthropic.api_key: "sk-ant-..."</div>
                <br />
                <div style={{ color: COLORS.muted }}># 2. Enable signal generation</div>
                <div style={{ color: COLORS.positive }}>integration_modes.signal_generation.enabled: true</div>
                <br />
                <div style={{ color: COLORS.muted }}># 3. Run the platform</div>
                <div style={{ color: COLORS.primary }}>python main.py --mode ai-signals</div>
                <br />
                <div style={{ color: COLORS.muted }}># 4. Claude will analyze your watchlist and generate signals</div>
                <div style={{ color: COLORS.muted }}># Signals appear in the dashboard and can auto-execute in paper mode</div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

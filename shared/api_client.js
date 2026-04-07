/**
 * Shared API Client - Used by both React Web and React Native
 * Provides typed methods for all backend endpoints + WebSocket management.
 *
 * Usage:
 *   import { ApiClient } from '../shared/api_client';
 *   const api = new ApiClient('http://localhost:8000');
 *   const signals = await api.scan();
 *   api.connectWebSocket('signals', (data) => console.log(data));
 */

const DEFAULT_BASE_URL = 'http://localhost:8000';

class ApiClient {
  constructor(baseUrl = DEFAULT_BASE_URL) {
    this.baseUrl = baseUrl;
    this.wsConnections = {};
    this.listeners = {};
  }

  // ---- HTTP Methods ----
  async _get(path, params = {}) {
    const url = new URL(`${this.baseUrl}${path}`);
    Object.entries(params).forEach(([k, v]) => url.searchParams.append(k, v));
    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
    return res.json();
  }

  async _post(path, body = {}) {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
    return res.json();
  }

  // ---- Health ----
  async health() { return this._get('/api/health'); }

  // ---- Strategies ----
  async getStrategies() { return this._get('/api/strategies'); }
  async activateStrategy(name, active) { return this._post('/api/strategies/activate', { name, active }); }
  async updateWeights(weights) { return this._post('/api/strategies/weights', { weights }); }

  // ---- Watchlist ----
  async getWatchlist() { return this._get('/api/watchlist'); }
  async addToWatchlist(ticker, high, low, sector) {
    return this._post('/api/watchlist/add', { ticker, high, low, sector });
  }

  // ---- Scanning & Signals ----
  async scan() { return this._get('/api/scan'); }

  // ---- Portfolio ----
  async getPortfolio() { return this._get('/api/portfolio'); }
  async executeTrade(ticker, action, shares, dollarAmount) {
    return this._post('/api/trade', { ticker, action, shares, dollar_amount: dollarAmount });
  }

  // ---- Backtesting ----
  async runBacktest(capital = 100000, days = 504) {
    return this._get('/api/backtest', { capital, days });
  }

  // ---- Performance ----
  async getPerformance() { return this._get('/api/performance'); }

  // ---- LLM ----
  async getLLMProviders() { return this._get('/api/llm/providers'); }
  async configureLLM(provider, model, apiKey, baseUrl) {
    return this._post('/api/llm/configure', { provider, model, api_key: apiKey, base_url: baseUrl });
  }
  async analyzeLLM(ticker, prompt, includePortfolio = false) {
    return this._post('/api/llm/analyze', { ticker, prompt, include_portfolio: includePortfolio });
  }

  // ---- WebSocket ----
  connectWebSocket(channel, onMessage, onError) {
    const wsUrl = this.baseUrl.replace('http', 'ws');
    const ws = new WebSocket(`${wsUrl}/ws/${channel}`);
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      onMessage(data);
      (this.listeners[channel] || []).forEach(fn => fn(data));
    };
    ws.onerror = (err) => onError?.(err);
    ws.onclose = () => {
      // Auto-reconnect after 5 seconds
      setTimeout(() => this.connectWebSocket(channel, onMessage, onError), 5000);
    };
    this.wsConnections[channel] = ws;
    return ws;
  }

  sendWsMessage(channel, data) {
    const ws = this.wsConnections[channel];
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
    }
  }

  addListener(channel, callback) {
    this.listeners[channel] = this.listeners[channel] || [];
    this.listeners[channel].push(callback);
  }

  disconnect() {
    Object.values(this.wsConnections).forEach(ws => ws.close());
    this.wsConnections = {};
  }
}

// Export for both CJS (React Native) and ESM (React Web)
if (typeof module !== 'undefined') module.exports = { ApiClient };
export { ApiClient };

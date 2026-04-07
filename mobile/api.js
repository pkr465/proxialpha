/**
 * Mobile API Client - wraps the shared ApiClient for React Native usage.
 * Handles connection management, error recovery, and state synchronization.
 */

const DEFAULT_BASE_URL = 'http://10.0.2.2:8000'; // Android emulator -> host
const IOS_BASE_URL = 'http://localhost:8000';

import { Platform } from 'react-native';

class MobileApiClient {
  constructor(customUrl) {
    this.baseUrl = customUrl || (Platform.OS === 'ios' ? IOS_BASE_URL : DEFAULT_BASE_URL);
    this.wsConnections = {};
    this.listeners = {};
    this.connected = false;
  }

  setBaseUrl(url) {
    this.disconnect();
    this.baseUrl = url;
  }

  // ---- HTTP ----
  async _get(path, params = {}) {
    const url = new URL(`${this.baseUrl}${path}`);
    Object.entries(params).forEach(([k, v]) => url.searchParams.append(k, String(v)));
    const res = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return res.json();
  }

  async _post(path, body = {}) {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return res.json();
  }

  // ---- Health ----
  async health() {
    try {
      const data = await this._get('/api/health');
      this.connected = true;
      return data;
    } catch (e) {
      this.connected = false;
      throw e;
    }
  }

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
    ws.onopen = () => { this.connected = true; };
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      onMessage(data);
      (this.listeners[channel] || []).forEach(fn => fn(data));
    };
    ws.onerror = (err) => { this.connected = false; onError?.(err); };
    ws.onclose = () => {
      this.connected = false;
      setTimeout(() => this.connectWebSocket(channel, onMessage, onError), 5000);
    };
    this.wsConnections[channel] = ws;
    return ws;
  }

  addListener(channel, callback) {
    this.listeners[channel] = this.listeners[channel] || [];
    this.listeners[channel].push(callback);
  }

  disconnect() {
    Object.values(this.wsConnections).forEach(ws => ws.close());
    this.wsConnections = {};
    this.connected = false;
  }
}

// Singleton instance
const api = new MobileApiClient();
export { MobileApiClient };
export default api;
